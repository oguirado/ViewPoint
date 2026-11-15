"""Multi-actor lease and region-scoped admission (reference implementation)."""

from .admission import (  # noqa: F401
    ALLOWED_GUI_KEYS,
    DISPLAY_H,
    DISPLAY_W,
    GUI_TURN_CAP_MS,
    SCRIPT_TURN_CAP_MS,
    AdmissionController,
    AdmissionDecision,
    Code,
    ExecutorOutcome,
    FrameObservation,
    Lease,
    PostCapture,
    Proposal,
    Rect,
    VerificationReceipt,
)
