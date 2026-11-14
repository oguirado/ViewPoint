"""ViewPoint readiness judge (Track P, Claude Fable 5.2). See judge.py and README.md."""
from .judge import (History, Judge, score_intent, score_queue, intent_sig, area_permille, union_area,  # noqa: F401
                    WEIGHTS, FRAME_RING, MAX_DROPS, LLM_CLAMP)
