"""PLAN: deterministic pipe-only planner policy and CLI for hosted observations."""

from wender.plan.policy import (
    DEFAULT_HOLDINGS_MAX_MS,
    DEFAULT_HOLDINGS_PROPOSAL_ID,
    DEFAULT_TICK_WINDOW,
    ELEMENT_TABLE_SCHEMA,
    EXIT_CODES,
    PlannerError,
    build_state_witness,
    canonical_json,
    is_generation_control,
    plan,
    plan_holdings,
    plan_holdings_step,
    project_vision_element_table,
    recheck_holdings_proposal,
    recheck_plan,
    select_target_index,
    validate_plan_response,
)

__all__ = [
    "DEFAULT_HOLDINGS_MAX_MS",
    "DEFAULT_HOLDINGS_PROPOSAL_ID",
    "DEFAULT_TICK_WINDOW",
    "ELEMENT_TABLE_SCHEMA",
    "EXIT_CODES",
    "PlannerError",
    "build_state_witness",
    "canonical_json",
    "is_generation_control",
    "main",
    "plan",
    "plan_holdings",
    "plan_holdings_step",
    "project_vision_element_table",
    "recheck_holdings_proposal",
    "recheck_plan",
    "select_target_index",
    "validate_plan_response",
]


def __getattr__(name: str):
    if name == "main":
        from wender.plan.cli import main
        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
