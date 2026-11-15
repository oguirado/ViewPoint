"""OpenSwitchboard & TaskLight Event Projection + Reconciler (`swarm.projection`).

Downstream implementation for ViewPoint Swarm unit H4 on branch `swarm-1114`.
Projects broker discrete simulation events from `events.jsonl` into:
1. OpenSwitchboard (`osb`) room `viewpoint-capsule` (§7.1)
2. TaskLight (`context-wall`) room `viewpoint` (§7.2)
Includes cursor-tracked incremental offset persistence and reconciler audit.
"""

from .projector import (
    ADMITTED_VERDICTS,
    REFUSAL_VERDICTS,
    TRACKED_AGENTS,
    BaseSink,
    CompositeSink,
    DryRunSink,
    OsbSink,
    Projector,
    ProjectionState,
    TaskLightMcpSink,
)
from .reconciler import (
    ReconciliationReport,
    Reconciler,
    audit_events,
)

__all__ = [
    "ADMITTED_VERDICTS",
    "REFUSAL_VERDICTS",
    "TRACKED_AGENTS",
    "BaseSink",
    "CompositeSink",
    "DryRunSink",
    "OsbSink",
    "Projector",
    "ProjectionState",
    "TaskLightMcpSink",
    "ReconciliationReport",
    "Reconciler",
    "audit_events",
]
