# @module {"id":"wa.perceive.godot_adapter","layer":1,"owner":"PERCEIVE","book":"HB-WA-s1-perceive","deps":["wa.perceive.godot","wa.perceive.session_registry","wa.perceive.godot_window_target"]}
"""Compatibility facade: public Godot T2 API without altered signatures or grammar.

The original validator currently accepts both dotted paths and screen-rooted slash
paths. GodotWindowTarget imposes the *stricter* dotted-path task requirement locally;
the public re-exports intentionally retain the original implementation.
"""
from .godot import (
    node_id_for_control_path,
    parse_ui_state,
    validate_uitree,
    read_outbox_batches,
    write_inbox_batch,
    GodotT2Client,
    OutboxReadResult,
)

try:
    from .session_registry import Handle, Observation, FrameRef
except ModuleNotFoundError as exc:
    if exc.name != "wender.perceive.session_registry":
        raise
    # The canonical provider is not in bridge/integration yet. These structural
    # fallbacks originate in this task's owned production module only.
    from .godot_window_target import Handle, Observation, FrameRef

__all__ = (
    "node_id_for_control_path",
    "parse_ui_state",
    "validate_uitree",
    "read_outbox_batches",
    "write_inbox_batch",
    "GodotT2Client",
    "OutboxReadResult",
    "Handle",
    "Observation",
    "FrameRef",
)
