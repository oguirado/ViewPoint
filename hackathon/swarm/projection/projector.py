"""OpenSwitchboard & TaskLight Event Projector (`swarm.projection.projector`).

Projects discrete simulation events from `events.jsonl` into:
1. OpenSwitchboard (`osb`) room `viewpoint-capsule` (§7.1)
2. TaskLight (`context-wall`) room `viewpoint` (§7.2)

Supports:
- Cursor-tracked incremental offset persistence (`--state-file`)
- Live follow mode (`--follow`)
- One-shot processing (`--once`)
- Task finalization and reconciler audit (`--finalize`)
- Pluggable sinks (`OsbSink`, `TaskLightMcpSink`, `DryRunSink`, `CompositeSink`)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Set

logger = logging.getLogger("swarm.projection")

# Canonical agents and verdicts per CONTRACT.md
TRACKED_AGENTS = ("modeler-agent", "props-agent", "look-agent", "camera-agent")

ADMITTED_VERDICTS = frozenset({
    "VERIFIED",
    "VERIFIED_WITH_SPILL",
    "STATE_VERIFIED",
    "NO_VISIBLE_CHANGE",
})

REFUSAL_VERDICTS = frozenset({
    "STALE_FRAME",
    "FRAME_EVICTED",
    "NO_LEASE",
    "LEASE_EXPIRED",
    "ACTUATOR_BUSY",
    "FOOTPRINT_MISSING",
    "TURN_TIMEOUT",
    "QUEUE_FULL",
    "DUPLICATE_EVENT",
    "CLAIM_REFUSED",
    "JOB_ERROR",
    "INPUT_UNCERTAIN",
    "OUTCOME_UNCERTAIN",
})

DEFAULT_OSB_BIN = os.environ.get("VP_OSB_CLI", "switchboard-skill/bin/osb")
DEFAULT_OSB_ROOM = "viewpoint-capsule"
DEFAULT_CW_ROOM = "viewpoint"
DEFAULT_REPO_PATH = os.environ.get("VP_CW_REPO", "FireBase_BuildHybridAI/repo")
DEFAULT_EVENTS_LOG = str(Path(os.environ.get("VP_HACKATHON_ROOT") or Path(__file__).resolve().parents[2]) / "swarm" / "run" / "events.jsonl")


def sanitize_osb_text(text: Any, max_bytes: int = 3800) -> str:
    """Sanitize text for osb post (m3, r1-2, r1-3):
    1. Convert to string.
    2. Strip/replace control characters (ord(ch) < 32 other than \n and \t, notably \r, plus U+007F DEL).
    3. Replace lone UTF-16 surrogates (0xD800..0xDFFF) with U+FFFD.
    4. Cap UTF-8 bytes at <= max_bytes (default 3800 <= 4096).
    """
    if text is None:
        text = ""
    else:
        text = str(text)

    # Normalize CRLF to LF, and isolated CR to space
    text = text.replace("\r\n", "\n").replace("\r", " ")

    # Strip/replace control characters (ord < 32 except \n/\t, plus DEL 127)
    # and replace lone UTF-16 surrogates with U+FFFD
    cleaned_chars: List[str] = []
    for ch in text:
        code = ord(ch)
        if (code < 32 and ch not in ("\n", "\t")) or code == 127:
            cleaned_chars.append(" ")
        elif 0xD800 <= code <= 0xDFFF:
            cleaned_chars.append("\ufffd")
        else:
            cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars)

    # Cap UTF-8 bytes at <= max_bytes
    encoded = cleaned.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        encoded = encoded[:max_bytes]
        cleaned = encoded.decode("utf-8", errors="ignore")

    return cleaned


def extract_park_target(record: Dict[str, Any]) -> str:
    """Extract event_id or agent_id from park control record, or parse from reason (m2)."""
    target = record.get("event_id") or record.get("agent_id")
    if target and str(target).strip():
        return str(target).strip()
    reason = str(record.get("reason", ""))
    if ":" in reason:
        # e.g. parked_after_3_stale_drops:<event_id>
        parts = reason.split(":", 1)
        parsed = parts[1].strip()
        if parsed:
            return parsed
    return "park"


def get_default_plan_steps(agent_id: str) -> List[Dict[str, str]]:
    """Return standard 12 steps matching TaskLight schema constraints."""
    return [
        {
            "text": f"Execute batch {i + 1} for {agent_id}"[:80],
            "accept": "State verified or visible change confirmed"[:160],
        }
        for i in range(12)
    ]


class ProjectionState:
    """Cursor-tracked state for incremental projection and resume."""

    def __init__(
        self,
        offset_bytes: int = 0,
        line_number: int = 0,
        prev_turn_agent: Optional[str] = None,
        planned_agents: Optional[Set[str]] = None,
        closed_agents: Optional[Set[str]] = None,
        step_indices: Optional[Dict[str, int]] = None,
        plan_generations: Optional[Dict[str, int]] = None,
        open_intents: Optional[Dict[str, List[str]]] = None,
        uncertain_events: Optional[List[str]] = None,
        refusals: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.offset_bytes = int(offset_bytes)
        self.line_number = int(line_number)
        self.prev_turn_agent = prev_turn_agent
        self.planned_agents = set(planned_agents or ())
        self.closed_agents = set(closed_agents or ())
        self.step_indices = dict(step_indices or {})
        self.plan_generations = dict(plan_generations or {})
        self.open_intents = {k: list(v) for k, v in (open_intents or {}).items()}
        self.uncertain_events = list(uncertain_events or ())
        self.refusals = list(refusals or ())

    def get_step_index(self, agent_id: str) -> int:
        return self.step_indices.get(agent_id, 0)

    def reset_step_index(self, agent_id: str) -> None:
        self.step_indices[agent_id] = 0

    def increment_step_index(self, agent_id: str) -> None:
        self.step_indices[agent_id] = self.step_indices.get(agent_id, 0) + 1

    def get_plan_generation(self, agent_id: str) -> int:
        return self.plan_generations.get(agent_id, 1)

    def advance_plan_generation(self, agent_id: str) -> int:
        gen = self.plan_generations.get(agent_id, 1) + 1
        self.plan_generations[agent_id] = gen
        self.step_indices[agent_id] = 0
        return gen

    def to_dict(self) -> Dict[str, Any]:
        return {
            "offset_bytes": self.offset_bytes,
            "line_number": self.line_number,
            "prev_turn_agent": self.prev_turn_agent,
            "planned_agents": sorted(self.planned_agents),
            "closed_agents": sorted(self.closed_agents),
            "step_indices": self.step_indices,
            "plan_generations": self.plan_generations,
            "open_intents": self.open_intents,
            "uncertain_events": self.uncertain_events,
            "refusals": self.refusals,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProjectionState:
        return cls(
            offset_bytes=data.get("offset_bytes", 0),
            line_number=data.get("line_number", 0),
            prev_turn_agent=data.get("prev_turn_agent"),
            planned_agents=set(data.get("planned_agents", ())),
            closed_agents=set(data.get("closed_agents", ())),
            step_indices=data.get("step_indices", {}),
            plan_generations=data.get("plan_generations", {}),
            open_intents=data.get("open_intents", {}),
            uncertain_events=data.get("uncertain_events", []),
            refusals=data.get("refusals", []),
        )

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f"{path.suffix}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str | Path) -> ProjectionState:
        path = Path(path)
        if not path.is_file():
            return cls()
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return cls.from_dict(data)


# ----------------------------------------------------------------------
# Pluggable Sinks
# ----------------------------------------------------------------------

class BaseSink:
    """Base class for projection sinks."""

    def ensure_room_opened(self, room: str) -> None:
        pass

    def post_osb(
        self,
        room: str,
        agent: str,
        act: str,
        text: str,
        ref: Optional[str] = None,
    ) -> None:
        pass

    def post_plan(
        self,
        room: str,
        agent: str,
        title: str,
        steps: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        return {"ok": True}

    def post_status(self, room: str, agent: str, text: str) -> Dict[str, Any]:
        return {"ok": True}

    def post_step(
        self,
        room: str,
        agent: str,
        index: int,
        state: str,
        outcome: Optional[str] = None,
        took: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {"ok": True}

    def close_task(self, room: str, agent: str, outcome: str) -> Dict[str, Any]:
        return {"ok": True}

    def close(self) -> None:
        pass


class DryRunSink(BaseSink):
    """In-memory sink recording all calls for fast, hermetic testing.
    Optionally mirrors TaskLight calls to cw_home if configured."""

    def __init__(
        self,
        cw_home: Optional[str | Path] = None,
        simulate_close_error: bool = False,
        error_agents: Optional[Set[str]] = None,
    ) -> None:
        self.osb_calls: List[Dict[str, Any]] = []
        self.mcp_calls: List[Dict[str, Any]] = []
        self.opened_rooms: Set[str] = set()
        self.cw_home = Path(cw_home) if cw_home else None
        self.simulate_close_error = simulate_close_error
        self.error_agents = set(error_agents or ())
        self._lamport = 0

    def _append_cw_event(
        self,
        room: str,
        agent: str,
        act: str,
        text: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.cw_home:
            return
        rdir = self.cw_home / "rooms" / room
        rdir.mkdir(parents=True, exist_ok=True)
        epath = rdir / "events.jsonl"
        if self._lamport == 0 and epath.is_file():
            try:
                with open(epath, "rb") as rf:
                    for raw_line in rf:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            existing = json.loads(raw_line.decode("utf-8", errors="replace"))
                            lam = existing.get("lamport")
                            if isinstance(lam, int) and not isinstance(lam, bool) and lam > self._lamport:
                                self._lamport = lam
                        except Exception:
                            pass
            except OSError:
                pass
        self._lamport += 1
        event: Dict[str, Any] = {
            "id": f"dry-{self._lamport}",
            "lamport": self._lamport,
            "ts": int(time.time() * 1000),
            "room": room,
            "agent": agent,
            "act": act,
            "text": text,
        }
        if extra:
            event.update(extra)
        with open(epath, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def ensure_room_opened(self, room: str) -> None:
        if room not in self.opened_rooms:
            self.opened_rooms.add(room)
            self.osb_calls.append({
                "act": "open",
                "room": room,
                "agent": "broker-projector",
                "text": "ViewPoint Swarm capsule projection room",
                "ref": None,
            })

    def post_osb(
        self,
        room: str,
        agent: str,
        act: str,
        text: str,
        ref: Optional[str] = None,
    ) -> None:
        sanitized_text = sanitize_osb_text(text)
        if act == "refute" and (not ref or not str(ref).strip()):
            raise ValueError(f"refute requires non-empty --ref, got {ref!r}")
        self.osb_calls.append({
            "room": room,
            "agent": agent,
            "act": act,
            "text": sanitized_text,
            "ref": ref,
        })

    def post_plan(
        self,
        room: str,
        agent: str,
        title: str,
        steps: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        call = {
            "tool": "post_plan",
            "room": room,
            "agent": agent,
            "title": title,
            "steps": steps,
        }
        self.mcp_calls.append(call)
        self._append_cw_event(room, agent, "plan", title, {"plan": {"title": title, "steps": steps}})
        return {"content": [{"type": "text", "text": json.dumps({"event": call})}], "isError": False}

    def post_status(self, room: str, agent: str, text: str) -> Dict[str, Any]:
        call = {
            "tool": "post_status",
            "room": room,
            "agent": agent,
            "text": text,
        }
        self.mcp_calls.append(call)
        self._append_cw_event(room, agent, "heartbeat", text)
        return {"content": [{"type": "text", "text": json.dumps({"event": call})}], "isError": False}

    def post_step(
        self,
        room: str,
        agent: str,
        index: int,
        state: str,
        outcome: Optional[str] = None,
        took: Optional[str] = None,
    ) -> Dict[str, Any]:
        call = {
            "tool": "post_step",
            "room": room,
            "agent": agent,
            "index": index,
            "state": state,
            "outcome": outcome,
            "took": took,
        }
        self.mcp_calls.append(call)
        step_payload: Dict[str, Any] = {"index": index, "state": state}
        if outcome is not None:
            step_payload["outcome"] = outcome
        if took is not None:
            step_payload["took"] = took
        self._append_cw_event(room, agent, "step", outcome or state, {"step": step_payload})
        return {"content": [{"type": "text", "text": json.dumps({"event": call})}], "isError": False}

    def close_task(self, room: str, agent: str, outcome: str) -> Dict[str, Any]:
        call = {
            "tool": "close_task",
            "room": room,
            "agent": agent,
            "outcome": outcome,
        }
        self.mcp_calls.append(call)
        if self.simulate_close_error or agent in self.error_agents:
            return {
                "content": [{"type": "text", "text": "close_task refused (simulated error)"}],
                "isError": True,
                "error": "close_task simulated failure",
            }
        self._append_cw_event(room, agent, "close", outcome)
        return {"content": [{"type": "text", "text": json.dumps({"event": call})}], "isError": False}


class OsbSink(BaseSink):
    """OpenSwitchboard CLI sink executing `osb post`."""

    def __init__(
        self,
        osb_bin: str = DEFAULT_OSB_BIN,
        xdg_runtime_dir: str = "/run/user/1000",
    ) -> None:
        self.osb_bin = osb_bin
        self.xdg_runtime_dir = xdg_runtime_dir
        self.checked_rooms: Set[str] = set()

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.xdg_runtime_dir:
            env["XDG_RUNTIME_DIR"] = self.xdg_runtime_dir
        # n6: Scrub OSB_LANE so osb lane policies do not restrict projector acts
        env.pop("OSB_LANE", None)
        return env

    def ensure_room_opened(self, room: str) -> None:
        if room in self.checked_rooms:
            return
        cmd_status = [
            "python3",
            "-I",
            self.osb_bin,
            "status",
            "--room",
            room,
        ]
        try:
            res = subprocess.run(
                cmd_status,
                env=self._env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            # rc == 4 means room not found -> auto-open
            if res.returncode == 4:
                logger.info("Room %r does not exist (rc 4); auto-opening with broker-projector", room)
                self.post_osb(
                    room=room,
                    agent="broker-projector",
                    act="open",
                    text="ViewPoint Swarm capsule projection room",
                )
            self.checked_rooms.add(room)
        except Exception as e:
            logger.warning("Failed checking room %s status: %s", room, e)

    def post_osb(
        self,
        room: str,
        agent: str,
        act: str,
        text: str,
        ref: Optional[str] = None,
    ) -> None:
        # m3: Sanitize text before every osb post call
        sanitized_text = sanitize_osb_text(text)
        if act == "refute" and (not ref or not str(ref).strip()):
            raise ValueError(f"refute requires non-empty --ref, got {ref!r}")

        cmd = [
            "python3",
            "-I",
            self.osb_bin,
            "post",
            "--room",
            room,
            "--agent",
            agent,
            "--act",
            act,
            "--text",
            sanitized_text,
        ]
        if ref:
            safe_ref = "".join(
                "\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else (" " if ord(ch) == 0 else ch)
                for ch in str(ref)
            )
            cmd.extend(["--ref", safe_ref])

        try:
            res = subprocess.run(
                cmd,
                env=self._env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            if res.returncode != 0:
                logger.warning(
                    "osb post returned rc %d: stdout=%s stderr=%s",
                    res.returncode,
                    res.stdout.strip(),
                    res.stderr.strip(),
                )
        except Exception as e:
            logger.error("Error executing osb post: %s", e)


class TaskLightMcpSink(BaseSink):
    """TaskLight stdio MCP client sink connecting to `bridge.room.mcp`."""

    def __init__(
        self,
        cw_home: Optional[str] = None,
        repo_path: str = DEFAULT_REPO_PATH,
    ) -> None:
        self.cw_home = cw_home or os.environ.get("CW_HOME", os.path.expanduser("~/.context-wall"))
        self.repo_path = repo_path
        self._proc: Optional[subprocess.Popen[str]] = None
        self._req_id = 0
        self._started = False

    def _start(self) -> None:
        if self._started and self._proc and self._proc.poll() is None:
            return
        env = dict(os.environ)
        env["PYTHONPATH"] = self.repo_path
        env["CW_HOME"] = self.cw_home

        self._proc = subprocess.Popen(
            ["python3", "-m", "bridge.room.mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        self._started = True

        # Send initialize handshake
        init_req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "projector", "version": "1.0"},
            },
        }
        self._send_raw(init_req)
        resp = self._read_raw()
        if "error" in resp:
            raise RuntimeError(f"MCP initialize failed: {resp['error']}")

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _send_raw(self, msg: Dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("MCP process stdin not available")
        line = json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n"
        line = line.encode("utf-8", errors="replace").decode("utf-8")
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

    def _read_raw(self) -> Dict[str, Any]:
        if not self._proc or not self._proc.stdout:
            raise RuntimeError("MCP process stdout not available")
        line = self._proc.stdout.readline()
        if not line:
            stderr = self._proc.stderr.read() if self._proc.stderr else ""
            raise RuntimeError(f"MCP process closed stdout unexpectedly. Stderr: {stderr}")
        return json.loads(line)

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self._start()
        req_id = self._next_id()
        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        }
        self._send_raw(msg)
        resp = self._read_raw()
        if "error" in resp:
            raise RuntimeError(f"MCP call_tool {name} error: {resp['error']}")
        result = resp.get("result", {})
        if result.get("isError"):
            logger.warning("MCP tool %s returned error result: %s", name, result)
        return result

    def post_plan(
        self,
        room: str,
        agent: str,
        title: str,
        steps: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        args = {
            "room": room,
            "agent": agent,
            "title": title[:120],
            "steps": [
                {"text": s["text"][:80], **({"accept": s["accept"][:160]} if "accept" in s else {})}
                for s in steps[:12]
            ],
        }
        return self.call_tool("post_plan", args)

    def post_status(self, room: str, agent: str, text: str) -> Dict[str, Any]:
        args = {
            "room": room,
            "agent": agent,
            "text": text,
        }
        return self.call_tool("post_status", args)

    def post_step(
        self,
        room: str,
        agent: str,
        index: int,
        state: str,
        outcome: Optional[str] = None,
        took: Optional[str] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {
            "room": room,
            "agent": agent,
            "index": max(0, int(index)),
            "state": state,
        }
        if outcome is not None:
            args["outcome"] = str(outcome)[:240]
        if took is not None:
            args["took"] = str(took)[:32]
        return self.call_tool("post_step", args)

    def close_task(self, room: str, agent: str, outcome: str) -> Dict[str, Any]:
        clean_outcome = " ".join(str(outcome).splitlines()).strip()
        if not clean_outcome:
            clean_outcome = f"Completed tasks for {agent}"
        args = {
            "room": room,
            "agent": agent,
            "outcome": clean_outcome[:240],
        }
        return self.call_tool("close_task", args)

    def close(self) -> None:
        if self._proc:
            try:
                if self._proc.stdin:
                    try:
                        self._proc.stdin.close()
                    except Exception:
                        pass
                if self._proc.stdout:
                    try:
                        self._proc.stdout.close()
                    except Exception:
                        pass
                if self._proc.stderr:
                    try:
                        self._proc.stderr.close()
                    except Exception:
                        pass
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                if self._proc.poll() is None:
                    self._proc.kill()
            self._proc = None
            self._started = False


class CompositeSink(BaseSink):
    """Multiplexes projection calls across multiple sinks."""

    def __init__(self, sinks: Sequence[BaseSink]) -> None:
        self.sinks = list(sinks)

    def ensure_room_opened(self, room: str) -> None:
        for s in self.sinks:
            s.ensure_room_opened(room)

    def post_osb(
        self,
        room: str,
        agent: str,
        act: str,
        text: str,
        ref: Optional[str] = None,
    ) -> None:
        for s in self.sinks:
            s.post_osb(room, agent, act, text, ref)

    def post_plan(
        self,
        room: str,
        agent: str,
        title: str,
        steps: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        res: Dict[str, Any] = {"ok": True}
        for s in self.sinks:
            r = s.post_plan(room, agent, title, steps)
            if r:
                res = r
        return res

    def post_status(self, room: str, agent: str, text: str) -> Dict[str, Any]:
        res: Dict[str, Any] = {"ok": True}
        for s in self.sinks:
            r = s.post_status(room, agent, text)
            if r:
                res = r
        return res

    def post_step(
        self,
        room: str,
        agent: str,
        index: int,
        state: str,
        outcome: Optional[str] = None,
        took: Optional[str] = None,
    ) -> Dict[str, Any]:
        res: Dict[str, Any] = {"ok": True}
        for s in self.sinks:
            r = s.post_step(room, agent, index, state, outcome, took)
            if r:
                res = r
        return res

    def close_task(self, room: str, agent: str, outcome: str) -> Dict[str, Any]:
        res: Dict[str, Any] = {"ok": True}
        for s in self.sinks:
            r = s.close_task(room, agent, outcome)
            if r:
                res = r
        return res

    def close(self) -> None:
        for s in self.sinks:
            s.close()


# ----------------------------------------------------------------------
# Projector Core
# ----------------------------------------------------------------------

class Projector:
    """Consumes events.jsonl and projects to OpenSwitchboard and TaskLight."""

    def __init__(
        self,
        events_file: str | Path = DEFAULT_EVENTS_LOG,
        state_file: Optional[str | Path] = None,
        sink: Optional[BaseSink] = None,
        osb_room: str = DEFAULT_OSB_ROOM,
        cw_room: str = DEFAULT_CW_ROOM,
        cw_home: Optional[str | Path] = None,
        repo_path: str = DEFAULT_REPO_PATH,
    ) -> None:
        self.events_file = Path(events_file)
        self.state_file = Path(state_file) if state_file else None
        self.osb_room = osb_room
        self.cw_room = cw_room
        self.cw_home = Path(cw_home) if cw_home else None
        self.last_finalize_errors: List[str] = []

        if sink is not None:
            self.sink = sink
            if isinstance(sink, DryRunSink) and self.cw_home and not sink.cw_home:
                sink.cw_home = self.cw_home
        else:
            self.sink = CompositeSink([
                OsbSink(),
                TaskLightMcpSink(cw_home=str(self.cw_home) if self.cw_home else None, repo_path=repo_path),
            ])

        # Load cursor state
        if self.state_file and self.state_file.is_file():
            self.state = ProjectionState.load(self.state_file)
        else:
            self.state = ProjectionState()

        # Auto-open room if needed
        self.sink.ensure_room_opened(self.osb_room)

    def process_record(self, record: Dict[str, Any]) -> bool:
        """Process a single JSON record from events.jsonl according to §7.1 and §7.2."""
        if not isinstance(record, dict):
            return False

        rec = record.get("rec")

        # §7.1: Skip cursor records
        if rec == "cursor":
            return False

        # §7.1: Skip claim (lease grant/renewal)
        if rec == "claim":
            return False

        # 1. Enqueue
        if rec == "enqueue":
            agent_id = record.get("agent_id", "")
            event_id = record.get("event_id", "")
            lane = record.get("lane", "")
            kind = record.get("kind", "")
            based_on_frame = record.get("based_on_frame", 0)

            # OSB projection
            osb_text = f"enqueue {event_id} ({lane}/{kind}) frame={based_on_frame}"
            self.sink.post_osb(
                room=self.osb_room,
                agent=agent_id,
                act="claim",
                text=osb_text,
                ref=event_id,
            )

            # TaskLight projection: on agent's FIRST enqueue, post_plan
            if agent_id in TRACKED_AGENTS and agent_id not in self.state.planned_agents:
                title = f"Task plan for {agent_id}"[:120]
                steps = get_default_plan_steps(agent_id)
                self.sink.post_plan(
                    room=self.cw_room,
                    agent=agent_id,
                    title=title,
                    steps=steps,
                )
                self.state.planned_agents.add(agent_id)

            intents = self.state.open_intents.setdefault(agent_id, [])
            if event_id and event_id not in intents:
                intents.append(event_id)
            return True

        # 2. Turn
        if rec == "turn":
            # n2: Skip zero-hold PASS turns (no ack, handoff, or post_status)
            if record.get("hold_ms") == 0:
                return False

            current_agent = record.get("agent_id", "")
            turn = record.get("turn", 0)
            turn_epoch = record.get("turn_epoch", turn)
            head_frame = record.get("head_frame", 0)

            # Token transfer check between consecutive turn records
            prev_agent = self.state.prev_turn_agent
            if prev_agent and prev_agent != current_agent:
                handoff_text = f"handoff -> {current_agent} (turn_epoch={turn_epoch})"
                self.sink.post_osb(
                    room=self.osb_room,
                    agent=prev_agent,
                    act="handoff",
                    text=handoff_text,
                    ref=f"turn-{turn_epoch}",
                )

            self.state.prev_turn_agent = current_agent

            # Turn granted ack
            turn_text = f"turn={turn} turn_epoch={turn_epoch} head={current_agent} frame={head_frame}"
            self.sink.post_osb(
                room=self.osb_room,
                agent="broker-projector",
                act="ack",
                text=turn_text,
                ref=f"turn-{turn_epoch}",
            )

            # TaskLight status
            if current_agent in TRACKED_AGENTS:
                self.sink.post_status(
                    room=self.cw_room,
                    agent=current_agent,
                    text=f"Acting on turn #{turn}",
                )
            return True

        # 3. Close
        if rec == "close":
            verdict = record.get("verdict", "")
            event_id = record.get("event_id")
            agent_id = record.get("agent_id", "")
            turn = record.get("turn", 0)

            # m4: Skip close early ONLY when verdict == "PASS" or event_id == "PASS"
            if verdict == "PASS" or event_id == "PASS":
                return False

            if verdict in ADMITTED_VERDICTS:
                tick = record.get("tick", 0)
                verify_frame = record.get("verify_frame_ref")
                ev_ref = str(event_id) if event_id else f"admitted-turn-{turn}-{agent_id}"
                evidence_text = f"close {event_id or ev_ref} verdict={verdict} tick={tick} verify_frame={verify_frame}"
                self.sink.post_osb(
                    room=self.osb_room,
                    agent=agent_id,
                    act="evidence",
                    text=evidence_text,
                    ref=ev_ref,
                )

                if agent_id in TRACKED_AGENTS:
                    # m1: 12-step rollover
                    step_idx = self.state.get_step_index(agent_id)
                    if step_idx >= 12:
                        gen = self.state.advance_plan_generation(agent_id)
                        title = f"Task plan for {agent_id} (cont. #{gen})"[:120]
                        steps = get_default_plan_steps(agent_id)
                        self.sink.post_plan(
                            room=self.cw_room,
                            agent=agent_id,
                            title=title,
                            steps=steps,
                        )
                        step_idx = 0

                    outcome = f"{event_id or ev_ref}: {verdict}"[:240]
                    self.sink.post_step(
                        room=self.cw_room,
                        agent=agent_id,
                        index=step_idx,
                        state="done",
                        outcome=outcome,
                    )
                    self.state.increment_step_index(agent_id)

                # Remove from open intents
                intents = self.state.open_intents.get(agent_id, [])
                if event_id and event_id in intents:
                    intents.remove(event_id)
                return True

            if verdict in REFUSAL_VERDICTS or verdict:
                # Refusal or error
                drops = record.get("drops", 0)
                reason = record.get("reason", "")
                if verdict not in REFUSAL_VERDICTS:
                    logger.warning("Unrecognized verdict %r mapped to refute/blocked for event %s", verdict, event_id)

                # m4 & m2: Synthesize deterministic non-empty ref if event_id is null/empty
                if event_id and str(event_id).strip():
                    ref_str = str(event_id).strip()
                else:
                    ref_str = f"refusal-turn-{turn}-{agent_id}"

                refute_text = f"refute {ref_str} verdict={verdict} drops={drops} reason={reason}"
                self.sink.post_osb(
                    room=self.osb_room,
                    agent="broker-projector",
                    act="refute",
                    text=refute_text,
                    ref=ref_str,
                )

                self.state.refusals.append({
                    "event_id": event_id or ref_str,
                    "agent_id": agent_id,
                    "verdict": verdict,
                    "ref": ref_str,
                })

                if agent_id in TRACKED_AGENTS:
                    # m1: 12-step rollover
                    step_idx = self.state.get_step_index(agent_id)
                    if step_idx >= 12:
                        gen = self.state.advance_plan_generation(agent_id)
                        title = f"Task plan for {agent_id} (cont. #{gen})"[:120]
                        steps = get_default_plan_steps(agent_id)
                        self.sink.post_plan(
                            room=self.cw_room,
                            agent=agent_id,
                            title=title,
                            steps=steps,
                        )
                        step_idx = 0

                    outcome = f"{event_id or ref_str}: {verdict}"[:240]
                    self.sink.post_step(
                        room=self.cw_room,
                        agent=agent_id,
                        index=step_idx,
                        state="blocked",
                        outcome=outcome,
                    )
                    self.state.increment_step_index(agent_id)

                if verdict in ("INPUT_UNCERTAIN", "OUTCOME_UNCERTAIN"):
                    target_uncertain = event_id or ref_str
                    if target_uncertain and target_uncertain not in self.state.uncertain_events:
                        self.state.uncertain_events.append(target_uncertain)
                return True

        # 4. Control
        if rec == "control":
            op = record.get("op", "")
            reason = record.get("reason", "")
            by = record.get("by", "director-agent")

            if op in ("stop", "resume"):
                ctrl_text = f"control op={op} reason={reason}"
                self.sink.post_osb(
                    room=self.osb_room,
                    agent=by or "director-agent",
                    act="verdict",
                    text=ctrl_text,
                    ref=f"control-{op}",
                )

            # M2: On stop, pause status instead of prematurely closing tasks
            if op == "stop":
                for agent_id in sorted(self.state.planned_agents):
                    if agent_id in TRACKED_AGENTS and agent_id not in self.state.closed_agents:
                        self.sink.post_status(
                            room=self.cw_room,
                            agent=agent_id,
                            text="Paused: broker control stop",
                        )

            # M2: On resume, post resumed status for active planned agents
            if op == "resume":
                for agent_id in sorted(self.state.planned_agents):
                    if agent_id in TRACKED_AGENTS and agent_id not in self.state.closed_agents:
                        self.sink.post_status(
                            room=self.cw_room,
                            agent=agent_id,
                            text="Resumed: broker control resume",
                        )

            if op == "park":
                # m2: extract park target from top-level fields or parse from reason
                target = extract_park_target(record)
                ref_str = str(target)
                drops = record.get("drops", 0)
                verdict = record.get("verdict", "PARK")
                refute_text = f"refute {ref_str} verdict={verdict} drops={drops} reason={reason}"
                self.sink.post_osb(
                    room=self.osb_room,
                    agent="broker-projector",
                    act="refute",
                    text=refute_text,
                    ref=ref_str,
                )

            if op == "reconcile":
                # Clear pending uncertain events (explicit reconcile only)
                self.state.uncertain_events.clear()

            return True

        return False

    def finalize_open_tasks(self, reason: str = "") -> None:
        """Close open tasks for all agents that have planned but not yet closed.
        M1: ONLY add agent_id to state.closed_agents if sink.close_task succeeded without error.
        r1-5: Isolate exceptions per agent so remaining open agents still get close_task attempts."""
        self.last_finalize_errors.clear()
        for agent_id in sorted(self.state.planned_agents):
            if agent_id in TRACKED_AGENTS and agent_id not in self.state.closed_agents:
                outcome = f"Completed capsule tasks for {agent_id}"
                if reason:
                    outcome += f" ({reason})"
                try:
                    res = self.sink.close_task(
                        room=self.cw_room,
                        agent=agent_id,
                        outcome=outcome[:240],
                    )
                except Exception as exc:
                    err_msg = f"close_task exception for {agent_id}: {exc}"
                    logger.error(err_msg)
                    self.last_finalize_errors.append(err_msg)
                    continue

                if isinstance(res, dict) and res.get("isError"):
                    err_msg = f"close_task failed for {agent_id}: {res}"
                    logger.error(err_msg)
                    self.last_finalize_errors.append(err_msg)
                else:
                    self.state.closed_agents.add(agent_id)

    def run_once(self) -> int:
        """Process available lines in events_file up to EOF.
        m5 / r1-4: Read in binary mode ('rb'), only process lines ending with b'\\n',
        hold offset on partial line or mid-character UTF-8 split.
        r1-3: Guard per-record projection so malformed strings never abort the run.
        m8: Only save state when offset_bytes or state actually changed."""
        if not self.events_file.is_file():
            logger.info("Events file %s does not exist yet.", self.events_file)
            return 0

        initial_offset = self.state.offset_bytes
        lines_processed = 0

        with open(self.events_file, "rb") as f:
            if self.state.offset_bytes > 0:
                f.seek(self.state.offset_bytes)

            while True:
                line_start_offset = f.tell()
                raw_line = f.readline()
                if not raw_line:
                    break
                # m5 / r1-4: Hold offset on partial line without trailing b"\n"
                if not raw_line.endswith(b"\n"):
                    f.seek(line_start_offset)
                    break

                stripped = raw_line.decode("utf-8", errors="replace").strip()
                if stripped:
                    try:
                        record = json.loads(stripped)
                    except json.JSONDecodeError as e:
                        logger.warning("Skipping invalid JSON at line %d: %s", self.state.line_number + 1, e)
                    else:
                        try:
                            self.process_record(record)
                        except Exception as e:
                            logger.warning(
                                "Skipping record at line %d due to projection error: %s",
                                self.state.line_number + 1,
                                e,
                            )

                self.state.offset_bytes = f.tell()
                self.state.line_number += 1
                lines_processed += 1

        # m8: Only save state if offset_bytes actually advanced
        if self.state_file and self.state.offset_bytes != initial_offset:
            self.state.save(self.state_file)

        return lines_processed

    def run_follow(self, poll_interval_s: float = 0.1, max_idle_s: Optional[float] = None) -> None:
        """Poll events_file for new lines until interrupted."""
        idle_time = 0.0
        try:
            while True:
                processed = self.run_once()
                if processed > 0:
                    idle_time = 0.0
                else:
                    time.sleep(poll_interval_s)
                    idle_time += poll_interval_s
                    if max_idle_s and idle_time >= max_idle_s:
                        break
        except KeyboardInterrupt:
            logger.info("Projection follow interrupted by user.")
        finally:
            if self.state_file:
                self.state.save(self.state_file)

    def finalize(self) -> Any:
        """Finalize open tasks and execute reconciliation audit."""
        self.finalize_open_tasks()
        if self.state_file:
            self.state.save(self.state_file)

        from .reconciler import Reconciler
        reconciler = Reconciler(
            events_file=self.events_file,
            cw_home=self.cw_home,
            cw_room=self.cw_room,
            state=self.state,
        )
        report = reconciler.audit()
        # R1: Propagate every close_task refusal/exception recorded during finalize_open_tasks
        for err in self.last_finalize_errors:
            report.add_anomaly({
                "code": "CLOSE_TASK_REFUSED",
                "details": f"CLOSE_TASK_REFUSED:{err}",
            })
        return report

    def close(self) -> None:
        self.sink.close()


# ----------------------------------------------------------------------
# CLI Interface
# ----------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenSwitchboard & TaskLight Event Projector (swarm.projection)",
    )
    parser.add_argument(
        "--events-file",
        default=os.environ.get("SWARM_EVENTS_LOG", DEFAULT_EVENTS_LOG),
        help="Path to authoritative events.jsonl",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="Path to cursor state JSON file for offset tracking",
    )
    parser.add_argument(
        "--cw-home",
        default=None,
        help="Context-wall home directory (defaults to CW_HOME or ~/.context-wall)",
    )
    parser.add_argument(
        "--osb-room",
        default=DEFAULT_OSB_ROOM,
        help="Target OpenSwitchboard room slug",
    )
    parser.add_argument(
        "--cw-room",
        default=DEFAULT_CW_ROOM,
        help="Target TaskLight context-wall room slug",
    )
    parser.add_argument(
        "--sink",
        choices=["all", "osb", "mcp", "dry-run"],
        default="all",
        help="Sink backend to use",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process events to EOF and exit",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Keep polling events.jsonl in daemon mode",
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Close open tasks and run reconciler audit on completion",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Instantiate chosen sink
    if args.sink == "dry-run":
        sink: BaseSink = DryRunSink(cw_home=args.cw_home)
    elif args.sink == "osb":
        sink = OsbSink()
    elif args.sink == "mcp":
        sink = TaskLightMcpSink(cw_home=args.cw_home)
    else:
        sink = CompositeSink([
            OsbSink(),
            TaskLightMcpSink(cw_home=args.cw_home),
        ])

    projector = Projector(
        events_file=args.events_file,
        state_file=args.state_file,
        sink=sink,
        osb_room=args.osb_room,
        cw_room=args.cw_room,
        cw_home=args.cw_home,
    )

    try:
        if args.follow:
            projector.run_follow()
        else:
            projector.run_once()

        if args.finalize:
            report = projector.finalize()
            print(json.dumps(report.to_dict(), indent=2))
            return 0 if report.clean else 1

        return 0
    finally:
        projector.close()


if __name__ == "__main__":
    sys.exit(main())
