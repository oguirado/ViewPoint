"""Off-display post-processing overlay compositor for ViewPoint Swarm (H3).

Renders the turn-queue strip, per-agent interpolated cursors, and standing
attribution caption strictly off-display onto recorded MP4 videos from
authoritative broker `events.jsonl` logs (`swarm/CONTRACT.md`).
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "AGENT_SPECS",
    "BrokerTimelineTracker",
    "CANONICAL_HEIGHT",
    "CANONICAL_WIDTH",
    "RING_ORDER",
    "STANDING_CAPTION",
    "TOP_STRIP_HEIGHT",
    "compose_video",
    "evaluate_agent_cursor",
    "filter_events_for_timeline",
    "load_events",
    "resolve_timeline",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        mod = importlib.import_module(".compositor", __name__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
