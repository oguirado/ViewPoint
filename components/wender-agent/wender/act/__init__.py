"""ACT: most structured path first; single actor, parallel readers.

[TARGET, unit decision] Exports the hosted actuation guard so `MV_HOSTED=1` or active
`wender.plan` execution refuses direct actuation before any CDP/AT-SPI/Godot/desktop I/O.
"""
from __future__ import annotations

from wender.act.hosted_guard import (
    ACT_IMPORT_REFUSAL_CODE,
    HOSTED_ENV_VAR,
    HOSTED_READONLY_CDP_METHODS,
    REFUSAL_CODE,
    ActuationError,
    HostedActuationRefused,
    assert_actuation_allowed,
    assert_cdp_method_allowed,
    assert_plan_has_no_act_imports,
    bind_action_error,
    check_actuation_allowed,
    guard_executor,
    guard_module_executors,
    hosted_guard_scope,
    hosted_reason,
    install_executor_guards,
    is_hosted,
    is_planner_entry_active,
    is_readonly_cdp_method,
    mark_planner_entry,
    planner_entry_scope,
    scan_plan_act_imports,
    set_hosted_mode,
)

__all__ = [
    "ACT_IMPORT_REFUSAL_CODE",
    "HOSTED_ENV_VAR",
    "HOSTED_READONLY_CDP_METHODS",
    "REFUSAL_CODE",
    "ActuationError",
    "HostedActuationRefused",
    "assert_actuation_allowed",
    "assert_cdp_method_allowed",
    "assert_plan_has_no_act_imports",
    "bind_action_error",
    "check_actuation_allowed",
    "guard_executor",
    "guard_module_executors",
    "hosted_guard_scope",
    "hosted_reason",
    "install_executor_guards",
    "is_hosted",
    "is_planner_entry_active",
    "is_readonly_cdp_method",
    "mark_planner_entry",
    "planner_entry_scope",
    "scan_plan_act_imports",
    "set_hosted_mode",
]
