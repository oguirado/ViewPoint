"""ViewPoint Swarm Multi-Agent Runner, Shared-Prefix Briefs & Intent SDK (H2)."""

from __future__ import annotations

from pathlib import Path

CONTRACT_VERSION = 1

REPO_ROOT = Path("~/Documentos/viewpoint-hackathon")
DEFAULT_FRAMES_DIR = Path("/dev/shm/vqa-cu/swarm/frames")
DEFAULT_INTENTS_DIR = Path("/dev/shm/vqa-cu/swarm/intents")
DEFAULT_INBOX_DIR = Path("/dev/shm/vqa-cu/swarm/inbox")
DEFAULT_EVENTS_LOG = REPO_ROOT / "swarm" / "run" / "events.jsonl"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "swarm" / "run" / "output"
DEFAULT_PROOF_DIR = REPO_ROOT / "swarm" / "proof"
DEFAULT_BRIEFS_DIR = REPO_ROOT / "swarm" / "run" / "agent_briefs"

CANONICAL_ROLES = (
    "modeler-agent",
    "props-agent",
    "look-agent",
    "camera-agent",
)

ALLOWED_GUI_KEYS = ("KP_1", "KP_3", "KP_7", "KP_4", "KP_Add", "Home")
DEFAULT_GUI_FOOTPRINT = (0, 80, 1580, 900)

AGENT_SPECS = {
    "modeler-agent": {
        "write_prefixes": ["col:CHAR_", "obj:CHAR_"],
        "allow_gui": False,
        "cursor_xy": [480, 420],
    },
    "props-agent": {
        "write_prefixes": ["col:PROPS_", "obj:PROP_"],
        "allow_gui": False,
        "cursor_xy": [1080, 620],
    },
    "look-agent": {
        "write_prefixes": ["mat:", "light:", "world:"],
        "allow_gui": False,
        "cursor_xy": [780, 300],
    },
    "camera-agent": {
        "write_prefixes": ["cam:", "render:"],
        "allow_gui": True,
        "cursor_xy": [800, 500],
    },
}

DEFAULT_WAIT_TIMEOUT_S = 540.0
STALE_MAX_DROPS = 3

VERDICT_SUCCESS = frozenset({"STATE_VERIFIED", "VERIFIED", "VERIFIED_WITH_SPILL"})
VERDICT_NO_VISIBLE_CHANGE = frozenset({"NO_VISIBLE_CHANGE"})
VERDICT_RETRYABLE_STALE = frozenset({"STALE_FRAME"})
VERDICT_QUEUE_REFUSAL = frozenset({"DUPLICATE_EVENT", "QUEUE_FULL"})
VERDICT_NON_REPEATABLE = frozenset({"INPUT_UNCERTAIN", "OUTCOME_UNCERTAIN"})

__all__ = [
    "AGENT_SPECS",
    "ALLOWED_GUI_KEYS",
    "CANONICAL_ROLES",
    "CONTRACT_VERSION",
    "DEFAULT_BRIEFS_DIR",
    "DEFAULT_EVENTS_LOG",
    "DEFAULT_FRAMES_DIR",
    "DEFAULT_GUI_FOOTPRINT",
    "DEFAULT_INBOX_DIR",
    "DEFAULT_INTENTS_DIR",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_PROOF_DIR",
    "DEFAULT_WAIT_TIMEOUT_S",
    "REPO_ROOT",
    "STALE_MAX_DROPS",
    "VERDICT_NON_REPEATABLE",
    "VERDICT_NO_VISIBLE_CHANGE",
    "VERDICT_QUEUE_REFUSAL",
    "VERDICT_RETRYABLE_STALE",
    "VERDICT_SUCCESS",
]
