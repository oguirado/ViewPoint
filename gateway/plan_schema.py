"""Validate the model-authored plan before it reaches the browser.

The model proposes; the host validates and labels. A closed ingress schema
prevents response fields from impersonating host authority, and unknown
step references are retained only as visibly "unmatched" evidence.
"""

from __future__ import annotations

from typing import Any

MAX_SUMMARY = 600
MAX_REASONING = 1500
MAX_STEPS = 12
MAX_TITLE = 160
MAX_DETAIL = 600
MAX_REFS = 8
MAX_REF_LENGTH = 32

PLAN_FIELDS = {"summary", "reasoning", "steps"}
STEP_FIELDS = {"title", "detail", "engine_step_refs"}


class PlanValidationError(ValueError):
    """The provider response is not an admissible plan."""


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise PlanValidationError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise PlanValidationError(f"{field} must not be empty")
    if len(text) > limit:
        raise PlanValidationError(f"{field} exceeds {limit} characters")
    return text


def _match_label(refs: list[str], known: set[str]) -> str:
    if not refs:
        return "no_refs"
    matches = [ref for ref in refs if ref in known]
    if len(matches) == len(refs):
        return "matched"
    if matches:
        return "partial"
    return "unmatched"


def validate_plan(raw: Any, replay_steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a normalized plan or raise PlanValidationError.

    Unknown ``engine_step_refs`` are not rejected: Gemini may propose a step
    that the recording does not contain. They are marked ``unmatched`` so the
    page can show "no matching execution" instead of implying evidence.
    """

    if not isinstance(raw, dict):
        raise PlanValidationError("plan must be a JSON object")
    if set(raw) != PLAN_FIELDS:
        raise PlanValidationError("plan contains unexpected or missing fields")

    summary = _text(raw["summary"], "summary", MAX_SUMMARY)
    reasoning = _text(raw["reasoning"], "reasoning", MAX_REASONING)

    raw_steps = raw["steps"]
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_STEPS:
        raise PlanValidationError(f"steps must contain 1..{MAX_STEPS} entries")

    known = {
        step["step_id"]
        for step in replay_steps
        if isinstance(step, dict) and isinstance(step.get("step_id"), str)
    }

    steps: list[dict[str, Any]] = []
    for index, item in enumerate(raw_steps):
        where = f"steps[{index}]"
        if not isinstance(item, dict):
            raise PlanValidationError(f"{where} must be an object")
        if set(item) != STEP_FIELDS:
            raise PlanValidationError(f"{where} contains unexpected or missing fields")
        title = _text(item["title"], f"{where}.title", MAX_TITLE)
        detail = _text(item["detail"], f"{where}.detail", MAX_DETAIL)
        refs_raw = item["engine_step_refs"]
        if not isinstance(refs_raw, list) or len(refs_raw) > MAX_REFS:
            raise PlanValidationError(f"{where}.engine_step_refs must be a list")
        refs: list[str] = []
        for ref in refs_raw:
            if not isinstance(ref, str):
                raise PlanValidationError(f"{where}.engine_step_refs must contain strings")
            ref = ref.strip()
            if not ref or len(ref) > MAX_REF_LENGTH:
                raise PlanValidationError(f"{where}.engine_step_refs has an invalid id")
            if ref not in refs:
                refs.append(ref)
        steps.append(
            {
                "title": title,
                "detail": detail,
                "engine_step_refs": refs,
                "ref_matches": [{"id": ref, "exists": ref in known} for ref in refs],
                "match": _match_label(refs, known),
            }
        )

    return {"summary": summary, "reasoning": reasoning, "steps": steps}
