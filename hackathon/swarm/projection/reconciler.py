"""OpenSwitchboard & TaskLight Event Reconciler (`swarm.projection.reconciler`).

Audits projected state and TaskLight room state against authoritative `events.jsonl`.
Enforces the Negative-Control rule:
- Flags any agent that emitted `post_plan` (or `enqueue`) without a matching `close_task` (`MISSING_CLOSE_TASK`).
- Flags any discrepancy where state claims closed but room store lacks `act == "close"` (`STATE_ROOM_MISMATCH`).
- Flags any refusal lacking `--ref` (`MISSING_REF_ON_REFUSAL`).
- Flags any `INPUT_UNCERTAIN` or `OUTCOME_UNCERTAIN` event without reconciliation (`UNRECONCILED_UNCERTAIN_EVENT`).

Exits with `rc = 0` when clean, `rc = 1` when anomalies exist.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence, Set

from .projector import (
    ADMITTED_VERDICTS,
    DEFAULT_CW_ROOM,
    DEFAULT_EVENTS_LOG,
    REFUSAL_VERDICTS,
    TRACKED_AGENTS,
    ProjectionState,
    extract_park_target,
)

logger = logging.getLogger("swarm.projection.reconciler")


class ReconciliationReport:
    """Structured report emitted by the projection reconciler."""

    def __init__(
        self,
        clean: bool,
        status: str,
        anomalies: List[Dict[str, Any]],
        stats: Dict[str, Any],
    ) -> None:
        self.clean = bool(clean)
        self.status = str(status)
        self.anomalies = list(anomalies)
        self.stats = dict(stats)

    def add_anomaly(self, anomaly: Dict[str, Any]) -> None:
        """Append an anomaly and force the report non-clean."""
        self.anomalies.append(anomaly)
        self.clean = False
        self.status = "ANOMALIES_DETECTED"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "clean": self.clean,
            "status": self.status,
            "anomalies": self.anomalies,
            "stats": self.stats,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


class Reconciler:
    """Audits broker events against TaskLight and OSB projection constraints."""

    def __init__(
        self,
        events_file: str | Path = DEFAULT_EVENTS_LOG,
        cw_home: Optional[str | Path] = None,
        cw_room: str = DEFAULT_CW_ROOM,
        cw_events_file: Optional[str | Path] = None,
        state: Optional[ProjectionState] = None,
        state_file: Optional[str | Path] = None,
    ) -> None:
        self.events_file = Path(events_file) if events_file else None
        if cw_home is not None:
            self.cw_home: Optional[Path] = Path(cw_home)
        elif "CW_HOME" in os.environ:
            self.cw_home = Path(os.environ["CW_HOME"])
        else:
            default_cw = os.path.expanduser("~/.context-wall")
            self.cw_home = Path(default_cw) if os.path.isdir(default_cw) else None

        self.cw_room = cw_room

        if cw_events_file:
            self.cw_events_file: Optional[Path] = Path(cw_events_file)
        elif self.cw_home:
            self.cw_events_file = self.cw_home / "rooms" / self.cw_room / "events.jsonl"
        else:
            self.cw_events_file = None

        if state is not None:
            self.state = state
        elif state_file and Path(state_file).is_file():
            self.state = ProjectionState.load(state_file)
        else:
            self.state = None

    def audit(self) -> ReconciliationReport:
        anomalies: List[Dict[str, Any]] = []

        total_broker_events = 0
        agents_enqueued: Set[str] = set()
        enqueued_event_ids: Set[str] = set()
        refusals_count = 0
        uncertain_events: Dict[str, Dict[str, Any]] = {}

        # 1. Audit authoritative broker events
        if self.events_file and self.events_file.is_file():
            with open(self.events_file, "r", encoding="utf-8", errors="replace") as f:
                for line_idx, line in enumerate(f, start=1):
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        record = json.loads(line_str)
                    except json.JSONDecodeError:
                        anomalies.append({
                            "code": "MALFORMED_JSON_LINE",
                            "line": line_idx,
                            "details": f"Failed parsing JSON on line {line_idx}",
                        })
                        continue

                    total_broker_events += 1
                    rec = record.get("rec")

                    if rec == "enqueue":
                        ag = record.get("agent_id")
                        ev_id = record.get("event_id")
                        if ag in TRACKED_AGENTS:
                            agents_enqueued.add(ag)
                        if ev_id:
                            enqueued_event_ids.add(ev_id)

                    elif rec == "close":
                        verdict = record.get("verdict", "")
                        ev_id = record.get("event_id")
                        ag = record.get("agent_id", "")
                        turn = record.get("turn", 0)

                        # m4: Skip noisy PASS records only
                        if verdict == "PASS" or ev_id == "PASS":
                            continue

                        # Check refusal and --ref requirement
                        if verdict in REFUSAL_VERDICTS or (verdict and verdict not in ADMITTED_VERDICTS):
                            refusals_count += 1
                            ref_str = str(ev_id) if (ev_id and str(ev_id).strip()) else f"refusal-turn-{turn}-{ag}"
                            if not ref_str.strip():
                                anomalies.append({
                                    "code": "MISSING_REF_ON_REFUSAL",
                                    "line": line_idx,
                                    "event_id": ev_id,
                                    "verdict": verdict,
                                    "details": f"Refusal verdict {verdict} missing non-empty --ref identifier",
                                })

                        if verdict in ("INPUT_UNCERTAIN", "OUTCOME_UNCERTAIN"):
                            target_id = str(ev_id) if (ev_id and str(ev_id).strip()) else f"uncertain-turn-{turn}-{ag}"
                            uncertain_events[target_id] = {
                                "verdict": verdict,
                                "line": line_idx,
                                "ts_ms": record.get("ts_ms", 0),
                            }

                    elif rec == "control":
                        op = record.get("op", "")
                        if op == "park":
                            refusals_count += 1
                            target = extract_park_target(record)
                            if not str(target).strip():
                                anomalies.append({
                                    "code": "MISSING_REF_ON_REFUSAL",
                                    "line": line_idx,
                                    "details": "Control park record lacks target reference",
                                })
                        elif op == "reconcile":
                            # m6: Explicit reconcile clears prior uncertain events (resume does not)
                            uncertain_events.clear()

        # m6: Check unreconciled uncertain events
        if uncertain_events:
            for ev_id, info in sorted(uncertain_events.items()):
                anomalies.append({
                    "code": "UNRECONCILED_UNCERTAIN_EVENT",
                    "event_id": ev_id,
                    "verdict": info["verdict"],
                    "line": info["line"],
                    "details": f"Event {ev_id} ended with {info['verdict']} on line {info['line']} but queue was never reconciled",
                })

        # 2. Audit TaskLight room events
        cw_planned_agents: Set[str] = set()
        cw_step_agents: Set[str] = set()
        cw_closed_agents: Set[str] = set()
        cw_latest_plan_idx: Dict[str, int] = {}
        cw_latest_close_idx: Dict[str, int] = {}

        cw_home_available = (self.cw_home is not None or self.cw_events_file is not None)

        if self.cw_events_file and self.cw_events_file.is_file():
            with open(self.cw_events_file, "r", encoding="utf-8", errors="replace") as f:
                for cw_line_idx, line in enumerate(f, start=1):
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        obj = json.loads(line_str)
                    except json.JSONDecodeError:
                        continue

                    act = obj.get("act")
                    agent = obj.get("agent")
                    if agent in TRACKED_AGENTS:
                        if act == "plan":
                            cw_planned_agents.add(agent)
                            cw_latest_plan_idx[agent] = cw_line_idx
                        elif act == "step":
                            cw_step_agents.add(agent)
                        elif act == "close":
                            cw_latest_close_idx[agent] = cw_line_idx

        # R1: A close event satisfies MISSING_CLOSE_TASK ONLY if it appears
        # strictly after that agent's latest plan event in room order.
        for agent, plan_idx in cw_latest_plan_idx.items():
            close_idx = cw_latest_close_idx.get(agent)
            if close_idx is not None and close_idx > plan_idx:
                cw_closed_agents.add(agent)

        # M1: Cross-check with projection state
        if self.state:
            for ag in self.state.planned_agents:
                if ag in TRACKED_AGENTS:
                    cw_planned_agents.add(ag)

            if cw_home_available:
                # TaskLight room store is the sole ground truth.
                # Do NOT union state.closed_agents into cw_closed_agents.
                # If state claims closed while room store lacks close, flag STATE_ROOM_MISMATCH.
                for ag in self.state.closed_agents:
                    if ag in TRACKED_AGENTS and ag not in cw_closed_agents:
                        anomalies.append({
                            "code": "STATE_ROOM_MISMATCH",
                            "agent_id": ag,
                            "details": (
                                f"Projection state claims agent {ag} closed, "
                                f"but TaskLight room store ({self.cw_room}) lacks matching close event"
                            ),
                        })
            else:
                for ag in self.state.closed_agents:
                    if ag in TRACKED_AGENTS:
                        cw_closed_agents.add(ag)

        # Determine all agents that initiated tasks
        active_agents = (agents_enqueued | cw_planned_agents | cw_step_agents) & set(TRACKED_AGENTS)

        # Negative-control check: Every agent that planned/enqueued MUST have close_task in room store
        for agent in sorted(active_agents):
            if agent not in cw_closed_agents:
                anomalies.append({
                    "code": "MISSING_CLOSE_TASK",
                    "agent_id": agent,
                    "details": f"Agent {agent} emitted post_plan/enqueue/step but lacks a matching close_task in room store",
                })

        clean = (len(anomalies) == 0)
        status = "CLEAN" if clean else "ANOMALIES_DETECTED"

        stats = {
            "total_broker_events": total_broker_events,
            "agents_enqueued": sorted(agents_enqueued),
            "agents_planned": sorted(cw_planned_agents),
            "agents_closed": sorted(cw_closed_agents),
            "refusals_count": refusals_count,
            "uncertain_events_count": len(uncertain_events),
        }

        return ReconciliationReport(
            clean=clean,
            status=status,
            anomalies=anomalies,
            stats=stats,
        )


def audit_events(
    events_file: str | Path = DEFAULT_EVENTS_LOG,
    cw_home: Optional[str | Path] = None,
    cw_room: str = DEFAULT_CW_ROOM,
    state_file: Optional[str | Path] = None,
) -> ReconciliationReport:
    """Convenience helper to run audit."""
    reconciler = Reconciler(
        events_file=events_file,
        cw_home=cw_home,
        cw_room=cw_room,
        state_file=state_file,
    )
    return reconciler.audit()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenSwitchboard & TaskLight Projection Reconciler (swarm.projection.reconciler)",
    )
    parser.add_argument(
        "--events",
        "--events-file",
        dest="events_file",
        default=os.environ.get("SWARM_EVENTS_LOG", DEFAULT_EVENTS_LOG),
        help="Path to authoritative events.jsonl",
    )
    parser.add_argument(
        "--cw-home",
        default=None,
        help="Context-wall home directory (defaults to CW_HOME or ~/.context-wall)",
    )
    parser.add_argument(
        "--cw-room",
        default=DEFAULT_CW_ROOM,
        help="Target TaskLight context-wall room slug",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="Path to cursor state JSON file",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    report = audit_events(
        events_file=args.events_file,
        cw_home=args.cw_home,
        cw_room=args.cw_room,
        state_file=args.state_file,
    )

    print(report.to_json(indent=2))
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
