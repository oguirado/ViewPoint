# @module {"id":"wa.act.hosted_guard","layer":1,"owner":"ACT","deps":[],"book":"HB-WA-s2-act"}
"""Hosted actuation guard for wender.act executors and static wender/plan import verification.

[TARGET, unit decision] When wender runs hosted inside MetaViewer (`MV_HOSTED=1`) or
through the pipe-only planner entry (`wender.plan`), direct actuation through any
`wender.act` executor is refused before any CDP, AT-SPI, Godot, or desktop I/O occurs.
Actuation authority remains exclusively `core admission -> daemon -> capsule`.

An in-process planner entry permanently latches hosted mode (including MV_HOSTED
in os.environ for subsequently spawned children); later actuation in that same
process remains refused. Raw stdlib side channels are not runtime-interposed.
"""
from __future__ import annotations

import ast
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import functools
import importlib.machinery
import os
from pathlib import Path
import sys
import threading
from types import ModuleType
from typing import Any

# @anchor:wa.act.hosted_guard.HOSTED_ENV_VAR
HOSTED_ENV_VAR = "MV_HOSTED"
# @anchor:wa.act.hosted_guard.REFUSAL_CODE
REFUSAL_CODE = "hosted_actuation_refused"
# @anchor:wa.act.hosted_guard.ACT_IMPORT_REFUSAL_CODE
ACT_IMPORT_REFUSAL_CODE = "act_import_forbidden"

# @anchor:wa.act.hosted_guard.HOSTED_READONLY_CDP_METHODS
HOSTED_READONLY_CDP_METHODS: frozenset[str] = frozenset({
    "Accessibility.disable",
    "Accessibility.enable",
    "Accessibility.getAXNodeAndAncestors",
    "Accessibility.getChildAXNodes",
    "Accessibility.getFullAXTree",
    "Accessibility.getPartialAXTree",
    "Accessibility.getRootAXNode",
    "Accessibility.queryAXTree",
    "Browser.getVersion",
    "Browser.getWindowBounds",
    "Browser.getWindowForTarget",
    "DOM.describeNode",
    "DOM.disable",
    "DOM.enable",
    "DOM.getBoxModel",
    "DOM.getContentQuads",
    "DOM.getDocument",
    "DOM.getFlattenedDocument",
    "DOM.getNodeForLocation",
    "Page.captureScreenshot",
    "Page.disable",
    "Page.enable",
    "Page.getFrameTree",
    "Page.getLayoutMetrics",
    "Page.getNavigationHistory",
    "Target.attachToTarget",
    "Target.detachFromTarget",
    "Target.getTargetInfo",
    "Target.getTargets",
})

_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_PLAN_MODULE_NAMES = frozenset({"wender.plan", "wender.plan.cli", "wender.plan.policy"})
_PLAN_FILE_SUFFIXES = (
    "/wender/plan/cli.py",
    "/wender/plan/__init__.py",
    "/wender/plan/policy.py",
)

_EXECUTOR_TARGETS: dict[str, tuple[str, ...]] = {
    "wender.act.actor": ("execute",),
    "wender.act.api_path": (
        "run_api_action",
        "BpyExampleAdapter.call",
        "FakeApiAdapter.call",
    ),
    "wender.act.godot_executor": (
        "GodotExecutor.dispatch",
        "GodotExecutor.dispatch_batch",
    ),
    "wender.act.godot_window_driver": (
        "GodotWindowDriver.execute",
        "GodotWindowDriver.confirm_completion",
    ),
    "wender.act.holdings_dispatch": (
        "dispatch_guarded",
        "dispatch_batch",
    ),
    "wender.act.jq3_t2_broker_client": (
        "submit_batch",
        "T2BrokerClient.submit_batch",
    ),
    "wender.act.multiwin_isolation_verifier": (
        "authorize_cdp_action",
        "verify_cdp_isolation",
        "authorize_godot_command",
        "verify_godot_isolation",
        "CdpIsolationVerifier.verify",
        "GodotIsolationVerifier.verify",
    ),
    "wender.act.proto1_action_policy": (
        "ActionPolicy.dispatch",
    ),
    "wender.act.pslot_action_gate": (
        "run_vision_action",
    ),
    "wender.act.w8u_client": (
        "W8UStepClient.execute",
    ),
}

_STATE_LOCK = threading.RLock()
_HOSTED_MODE_OVERRIDE: bool | None = None
_HOSTED_MODE_LATCHED: bool = False
_PLANNER_ENTRY_DEPTH: int = 0
_PLANNER_SCOPE_DEPTH: int = 0
_PLANNER_ENTRY_LATCHED: bool = False
_FINDER_TLS = threading.local()


# @anchor:wa.act.hosted_guard.ActuationError
class ActuationError(ValueError):
    """Heap-allocated base error for wender.act actuation refusals."""


# @anchor:wa.act.hosted_guard.HostedActuationRefused
class HostedActuationRefused(ActuationError, RuntimeError):
    """Typed refusal raised before any wender.act executor I/O while hosted."""

    # @anchor:wa.act.hosted_guard.HostedActuationRefused.__init__
    def __init__(
        self,
        detail: str,
        *,
        code: str = REFUSAL_CODE,
        entry: str = "wender.act",
        reason: str = "MV_HOSTED=1",
    ) -> None:
        self.code = code
        self.entry = entry
        self.reason = reason
        self.detail = detail
        super().__init__(f"{code}: {detail}")


# @anchor:wa.act.hosted_guard.bind_action_error
def bind_action_error(action_error_cls: type[ValueError]) -> None:
    """Bind cdp_executor.ActionError into HostedActuationRefused.__bases__ once."""
    if (
        isinstance(action_error_cls, type)
        and issubclass(action_error_cls, ActuationError)
        and HostedActuationRefused.__bases__[0] is ActuationError
    ):
        HostedActuationRefused.__bases__ = (action_error_cls, RuntimeError)


def _env_hosted_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    raw = source.get(HOSTED_ENV_VAR)
    if not isinstance(raw, str):
        return False
    return raw.strip().lower() in _TRUTHY_ENV_VALUES


def _main_is_planner_entry() -> bool:
    main_mod = sys.modules.get("__main__")
    if main_mod is None:
        return False
    spec = getattr(main_mod, "__spec__", None)
    spec_name = getattr(spec, "name", None)
    if isinstance(spec_name, str) and (
        spec_name in _PLAN_MODULE_NAMES or spec_name.startswith("wender.plan.")
    ):
        return True
    main_file = getattr(main_mod, "__file__", None)
    if isinstance(main_file, str):
        norm = main_file.replace("\\", "/")
        if norm.endswith(_PLAN_FILE_SUFFIXES):
            return True
    return False


def _planner_on_call_stack() -> bool:
    try:
        frame = sys._getframe(1)
    except ValueError:
        return False
    while frame is not None:
        mod_name = frame.f_globals.get("__name__")
        if isinstance(mod_name, str) and (
            mod_name in _PLAN_MODULE_NAMES or mod_name.startswith("wender.plan.")
        ):
            return True
        frame = frame.f_back
    return False


def _cli_planner_depth() -> int:
    val = getattr(sys, "_wender_planner_entry_depth", 0)
    return val if isinstance(val, int) and not isinstance(val, bool) and val > 0 else 0


def _cli_hosted_latched() -> bool:
    return getattr(sys, "_wender_hosted_latched", False) is True


# @anchor:wa.act.hosted_guard.is_planner_entry_active
def is_planner_entry_active() -> bool:
    """Return True when the process or active call stack is inside wender.plan."""
    with _STATE_LOCK:
        if _PLANNER_ENTRY_DEPTH > 0 or _PLANNER_SCOPE_DEPTH > 0 or _PLANNER_ENTRY_LATCHED:
            return True
    if _cli_planner_depth() > 0:
        return True
    return _main_is_planner_entry() or _planner_on_call_stack()


# @anchor:wa.act.hosted_guard.hosted_reason
def hosted_reason(*, env: Mapping[str, str] | None = None) -> str | None:
    """Return the active trigger; a supplied env may add hosted mode, never clear it."""
    with _STATE_LOCK:
        override = _HOSTED_MODE_OVERRIDE
        latched = _HOSTED_MODE_LATCHED
        planner_depth = _PLANNER_ENTRY_DEPTH + _PLANNER_SCOPE_DEPTH
        planner_latched = _PLANNER_ENTRY_LATCHED
    if override is True or latched or _cli_hosted_latched():
        return "hosted_mode_override"
    if _env_hosted_enabled() or _env_hosted_enabled(env):
        return "MV_HOSTED=1"
    if (
        planner_depth > 0
        or planner_latched
        or _cli_planner_depth() > 0
        or _main_is_planner_entry()
        or _planner_on_call_stack()
    ):
        return "planner_entry"
    return None


# @anchor:wa.act.hosted_guard.is_hosted
def is_hosted(*, env: Mapping[str, str] | None = None) -> bool:
    """Return True when hosted guard is active via MV_HOSTED=1, override, or planner entry."""
    return hosted_reason(env=env) is not None


# @anchor:wa.act.hosted_guard.set_hosted_mode
def set_hosted_mode(enabled: bool | None) -> bool | None:
    """Latch process-local hosted-mode override when True; never clear once hosted."""
    if enabled is not None and not isinstance(enabled, bool):
        raise TypeError("hosted mode override must be a bool or None")
    global _HOSTED_MODE_OVERRIDE, _HOSTED_MODE_LATCHED
    with _STATE_LOCK:
        prev = _HOSTED_MODE_OVERRIDE
        if enabled is True or _HOSTED_MODE_LATCHED or _cli_hosted_latched():
            _HOSTED_MODE_LATCHED = True
            _HOSTED_MODE_OVERRIDE = True
        else:
            _HOSTED_MODE_OVERRIDE = None
        return prev


# @anchor:wa.act.hosted_guard.mark_planner_entry
def mark_planner_entry(active: bool = True) -> bool:
    """Latch planner-entry execution when active is True; public False cannot clear an active latch."""
    if not isinstance(active, bool):
        raise TypeError("active must be a bool")
    global _PLANNER_ENTRY_DEPTH, _PLANNER_ENTRY_LATCHED
    with _STATE_LOCK:
        if active:
            _PLANNER_ENTRY_LATCHED = True
            _PLANNER_ENTRY_DEPTH = max(1, _PLANNER_ENTRY_DEPTH + 1)
        elif not _PLANNER_ENTRY_LATCHED:
            _PLANNER_ENTRY_DEPTH = 0
        return (
            _PLANNER_ENTRY_DEPTH > 0
            or _PLANNER_SCOPE_DEPTH > 0
            or _PLANNER_ENTRY_LATCHED
            or _cli_planner_depth() > 0
        )


# @anchor:wa.act.hosted_guard.hosted_guard_scope
@contextmanager
def hosted_guard_scope(enabled: bool = True) -> Iterator[None]:
    """Context manager entering sticky hosted guard state when enabled is True."""
    set_hosted_mode(enabled)
    try:
        yield
    finally:
        set_hosted_mode(None)


# @anchor:wa.act.hosted_guard.planner_entry_scope
@contextmanager
def planner_entry_scope() -> Iterator[None]:
    """Context manager marking active execution inside the hosted planner entry."""
    global _PLANNER_SCOPE_DEPTH
    with _STATE_LOCK:
        _PLANNER_SCOPE_DEPTH += 1
    try:
        yield
    finally:
        with _STATE_LOCK:
            _PLANNER_SCOPE_DEPTH = max(0, _PLANNER_SCOPE_DEPTH - 1)


# @anchor:wa.act.hosted_guard.is_readonly_cdp_method
def is_readonly_cdp_method(method: str) -> bool:
    """Return True iff the CDP method is in the explicit read-only perception allowlist."""
    return isinstance(method, str) and method in HOSTED_READONLY_CDP_METHODS


# @anchor:wa.act.hosted_guard.assert_cdp_method_allowed
def assert_cdp_method_allowed(
    method: str,
    *,
    entry: str = "CdpPipe.send",
    env: Mapping[str, str] | None = None,
) -> None:
    """Allow read-only perception CDP methods while hosted; refuse any write/dispatch method."""
    if is_readonly_cdp_method(method):
        return
    label = f"{entry}:{method}" if isinstance(method, str) and method.strip() else entry
    assert_actuation_allowed(label, env=env)


# @anchor:wa.act.hosted_guard.assert_actuation_allowed
def assert_actuation_allowed(
    entry: str = "wender.act",
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Raise HostedActuationRefused before any executor I/O if hosted or in planner entry."""
    if not isinstance(entry, str) or not entry.strip():
        entry = "wender.act"
    reason = hosted_reason(env=env)
    if reason is not None:
        raise HostedActuationRefused(
            f"direct wender.act actuation via {entry} is forbidden while hosted ({reason}); "
            "actuation must flow through MetaViewer core admission",
            code=REFUSAL_CODE,
            entry=entry,
            reason=reason,
        )


# @anchor:wa.act.hosted_guard.check_actuation_allowed
def check_actuation_allowed(
    entry: str = "wender.act",
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Alias for assert_actuation_allowed."""
    assert_actuation_allowed(entry, env=env)


# @anchor:wa.act.hosted_guard.guard_executor
def guard_executor(entry: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator wrapping an executor callable with assert_actuation_allowed(entry)."""
    if not isinstance(entry, str) or not entry.strip():
        raise ValueError("entry must be a non-empty string")

    def _decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        if getattr(fn, "_wa_hosted_guarded", False):
            return fn

        @functools.wraps(fn)
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            assert_actuation_allowed(entry)
            return fn(*args, **kwargs)

        _wrapped._wa_hosted_guarded = True  # type: ignore[attr-defined]
        _wrapped._wa_hosted_entry = entry  # type: ignore[attr-defined]
        return _wrapped

    return _decorate


# @anchor:wa.act.hosted_guard.guard_module_executors
def guard_module_executors(module: ModuleType) -> None:
    """Wrap registered executor entry points on a wender.act submodule idempotently."""
    mod_name = getattr(module, "__name__", "")
    targets = _EXECUTOR_TARGETS.get(mod_name, ())
    short_mod = mod_name.rsplit(".", 1)[-1]
    for qualname in targets:
        entry_label = f"{short_mod}.{qualname}"
        if "." in qualname:
            cls_name, method_name = qualname.split(".", 1)
            owner = getattr(module, cls_name, None)
            if owner is None:
                continue
            current = getattr(owner, method_name, None)
            if callable(current) and not getattr(current, "_wa_hosted_guarded", False):
                setattr(owner, method_name, guard_executor(entry_label)(current))
        else:
            current = getattr(module, qualname, None)
            if callable(current) and not getattr(current, "_wa_hosted_guarded", False):
                setattr(module, qualname, guard_executor(entry_label)(current))


class _WenderActPostImportFinder:
    """MetaPathFinder that wraps executor entry points when wender.act submodules load."""

    def find_spec(
        self,
        fullname: str,
        path: Any = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname not in _EXECUTOR_TARGETS or getattr(_FINDER_TLS, "active", False):
            return None
        _FINDER_TLS.active = True
        try:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        finally:
            _FINDER_TLS.active = False
        if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
            return spec
        orig_loader = spec.loader
        if getattr(orig_loader, "_wa_hosted_loader", False):
            return spec

        class _GuardedLoader:
            _wa_hosted_loader = True

            def __getattr__(self, name: str) -> Any:
                return getattr(orig_loader, name)

            def create_module(self, sub_spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
                if hasattr(orig_loader, "create_module"):
                    return orig_loader.create_module(sub_spec)
                return None

            def exec_module(self, module: ModuleType) -> None:
                module.__loader__ = orig_loader
                if getattr(module, "__spec__", None) is not None:
                    module.__spec__.loader = orig_loader
                orig_loader.exec_module(module)
                guard_module_executors(module)

        spec.loader = _GuardedLoader()  # type: ignore[assignment]
        return spec


_POST_IMPORT_FINDER = _WenderActPostImportFinder()


# @anchor:wa.act.hosted_guard.install_executor_guards
def install_executor_guards() -> None:
    """Install the wender.act post-import guard hook and guard already-imported submodules."""
    with _STATE_LOCK:
        if _POST_IMPORT_FINDER not in sys.meta_path:
            sys.meta_path.insert(0, _POST_IMPORT_FINDER)
        for mod_name in _EXECUTOR_TARGETS:
            mod = sys.modules.get(mod_name)
            if isinstance(mod, ModuleType):
                guard_module_executors(mod)


def _is_forbidden_act_module(name: str) -> bool:
    return name == "wender.act" or name.startswith("wender.act.")


def _resolve_import_from_targets(
    node: ast.ImportFrom,
    package_parts: list[str],
) -> list[str]:
    level = node.level or 0
    mod = node.module or ""
    if level == 0:
        targets = [mod] if mod else []
        for alias in node.names:
            targets.append(f"{mod}.{alias.name}" if mod else alias.name)
        return targets

    if level <= len(package_parts):
        base_parts = package_parts[: len(package_parts) - level + 1]
    else:
        base_parts = ["wender"]
    if mod:
        base_parts = base_parts + mod.split(".")
    base = ".".join(part for part in base_parts if part)
    targets = [base] if base else []
    for alias in node.names:
        targets.append(f"{base}.{alias.name}" if base else alias.name)
    return targets


def _infer_package_parts(file_path: Path, root_dir: Path) -> list[str]:
    resolved = file_path.resolve()
    parts = list(resolved.with_suffix("").parts[:-1])
    if "wender" in parts:
        idx = len(parts) - 1 - parts[::-1].index("wender")
        return parts[idx:]
    try:
        rel_parts = list(resolved.relative_to(root_dir.resolve()).with_suffix("").parts[:-1])
    except ValueError:
        rel_parts = []
    if len(rel_parts) >= 2 and rel_parts[0] == "wender":
        return rel_parts
    if rel_parts and rel_parts[0] == "plan":
        return ["wender"] + rel_parts
    return ["wender", "plan"] + rel_parts


def _eval_static_str(node: ast.AST, env: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in env:
        return env[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _eval_static_str(node.left, env)
        right = _eval_static_str(node.right, env)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        pieces: list[str] = []
        for val in node.values:
            if isinstance(val, ast.FormattedValue):
                piece = _eval_static_str(val.value, env)
            else:
                piece = _eval_static_str(val, env)
            if piece is None:
                return None
            pieces.append(piece)
        return "".join(pieces)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
        and isinstance(node.args[0], (ast.List, ast.Tuple))
    ):
        sep = _eval_static_str(node.func.value, env)
        if sep is not None:
            items: list[str] = []
            for elt in node.args[0].elts:
                piece = _eval_static_str(elt, env)
                if piece is None:
                    return None
                items.append(piece)
            return sep.join(items)
    return None


def _collect_static_str_bindings(tree: ast.AST) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            val = _eval_static_str(node.value, bindings)
            if val is not None:
                bindings[node.targets[0].id] = val
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            val = _eval_static_str(node.value, bindings)
            if val is not None:
                bindings[node.target.id] = val
    return bindings


def _normalize_dynamic_target(raw_target: str, pkg_parts: list[str]) -> str:
    cleaned = raw_target.strip()
    if cleaned.startswith("."):
        level = len(cleaned) - len(cleaned.lstrip("."))
        stripped = cleaned.lstrip(".")
        if level <= len(pkg_parts):
            base_parts = pkg_parts[: len(pkg_parts) - level + 1]
        else:
            base_parts = ["wender"]
        if stripped:
            base_parts = base_parts + stripped.split(".")
        return ".".join(p for p in base_parts if p)
    return cleaned


def _dotted_attr_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    curr: ast.AST | None = node
    while isinstance(curr, ast.Attribute):
        parts.append(curr.attr)
        curr = curr.value
    if isinstance(curr, ast.Name):
        parts.append(curr.id)
        return ".".join(reversed(parts))
    return None


def _resolve_wender_module_file(
    dotted: str,
    base_dir: Path,
    wender_root: Path,
) -> Path | None:
    if not dotted.startswith("wender.") or _is_forbidden_act_module(dotted):
        return None
    sub_parts = dotted.split(".")[1:]
    if not sub_parts:
        return None

    search_roots: list[Path] = []
    if base_dir.name == "plan":
        if sub_parts[0] == "plan":
            plan_rel = Path(*sub_parts[1:]) if len(sub_parts) > 1 else Path("__init__")
            for cand in (base_dir / f"{plan_rel}.py", base_dir / plan_rel / "__init__.py"):
                if cand.is_file():
                    return cand.resolve()
        search_roots.append(base_dir.parent)
    search_roots.extend([base_dir, base_dir / "wender", wender_root])

    rel_mod = Path(*sub_parts)
    for root in search_roots:
        for cand in (root / f"{rel_mod}.py", root / rel_mod / "__init__.py"):
            if cand.is_file():
                return cand.resolve()
    return None


# @anchor:wa.act.hosted_guard.scan_plan_act_imports
def scan_plan_act_imports(
    plan_dir: str | os.PathLike[str] | None = None,
) -> list[dict[str, object]]:
    """Statically scan wender/plan/*.py and its transitive wender.* deps for forbidden act loads."""
    wender_root = Path(__file__).resolve().parents[1]
    default_plan_dir = wender_root / "plan"
    target_path = default_plan_dir if plan_dir is None else Path(plan_dir)
    if target_path.is_file():
        initial_files = [target_path.resolve()]
        base_dir = target_path.resolve().parent
    elif target_path.is_dir():
        initial_files = sorted(p.resolve() for p in target_path.rglob("*.py") if p.is_file())
        base_dir = target_path.resolve()
    else:
        raise FileNotFoundError(f"planner path does not exist: {target_path}")

    queue: list[Path] = list(initial_files)
    visited: set[Path] = set()
    violations: list[dict[str, object]] = []

    while queue:
        path = queue.pop(0)
        if path in visited:
            continue
        visited.add(path)

        try:
            rel_name = path.relative_to(base_dir).as_posix()
        except ValueError:
            try:
                rel_name = path.relative_to(wender_root.parent).as_posix()
            except ValueError:
                rel_name = path.name

        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=rel_name)
        pkg_parts = _infer_package_parts(path, base_dir)
        str_env = _collect_static_str_bindings(tree)
        # Resolve imported callable/module aliases conservatively across the file.
        import_aliases: dict[str, str] = {}
        for imported in ast.walk(tree):
            if isinstance(imported, ast.ImportFrom) and imported.module and not imported.level:
                for alias in imported.names:
                    import_aliases[alias.asname or alias.name] = f"{imported.module}.{alias.name}"
            elif isinstance(imported, ast.Import):
                for alias in imported.names:
                    if alias.asname:
                        import_aliases[alias.asname] = alias.name

        for node in ast.walk(tree):
            lineno = getattr(node, "lineno", 1)
            candidates: list[str] = []
            dep_candidates: list[str] = []

            if isinstance(node, ast.Import):
                candidates = [alias.name for alias in node.names]
                dep_candidates = list(candidates)
            elif isinstance(node, ast.ImportFrom):
                candidates = _resolve_import_from_targets(node, pkg_parts)
                dep_candidates = list(candidates)
            elif isinstance(node, ast.Attribute):
                chain = _dotted_attr_chain(node)
                if chain and _is_forbidden_act_module(chain):
                    candidates.append(chain)
            elif isinstance(node, ast.Subscript):
                owner_chain = _dotted_attr_chain(node.value)
                if owner_chain in ("sys.modules", "modules"):
                    key_str = _eval_static_str(node.slice, str_env)
                    if key_str is not None:
                        candidates.append(_normalize_dynamic_target(key_str, pkg_parts))
            elif isinstance(node, ast.Call):
                func = node.func
                func_chain = _dotted_attr_chain(func)
                if func_chain:
                    head, dot, tail = func_chain.partition(".")
                    func_chain = import_aliases.get(head, head) + dot + tail
                getattr_name = None
                if (
                    isinstance(func, ast.Call)
                    and isinstance(func.func, ast.Name)
                    and func.func.id == "getattr"
                    and len(func.args) >= 2
                ):
                    getattr_name = _eval_static_str(func.args[1], str_env)
                is_dyn_import = (
                    (isinstance(func, ast.Name) and func.id in ("__import__", "import_module"))
                    or (isinstance(func, ast.Attribute) and func.attr in ("__import__", "import_module"))
                    or func_chain == "importlib.import_module"
                    or getattr_name in ("__import__", "import_module")
                )
                is_exec_or_loader = (
                    (isinstance(func, ast.Name) and func.id in ("exec", "eval", "compile"))
                    or (isinstance(func, ast.Name) and func.id in (
                        "spec_from_file_location", "module_from_spec", "exec_module", "load_module", "SourceFileLoader"
                    ))
                    or getattr_name in ("exec", "eval", "compile")
                    or func_chain in (
                        "builtins.exec",
                        "builtins.eval",
                        "builtins.compile",
                        "importlib.util.spec_from_file_location",
                        "importlib.util.module_from_spec",
                        "importlib.machinery.SourceFileLoader",
                    )
                    or (
                        isinstance(func, ast.Attribute)
                        and func.attr in ("spec_from_file_location", "module_from_spec", "exec_module", "load_module", "SourceFileLoader")
                    )
                )
                if is_exec_or_loader:
                    candidates.append("wender.act.<dynamic_exec>")
                elif is_dyn_import:
                    if node.args:
                        resolved_arg = _eval_static_str(node.args[0], str_env)
                        if resolved_arg is not None:
                            norm_target = _normalize_dynamic_target(resolved_arg, pkg_parts)
                            candidates.append(norm_target)
                            dep_candidates.append(norm_target)
                        else:
                            candidates.append("wender.act.<dynamic_import>")
                    else:
                        candidates.append("wender.act.<dynamic_import>")
                elif isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
                    owner_chain = _dotted_attr_chain(node.args[0])
                    attr_str = _eval_static_str(node.args[1], str_env)
                    if owner_chain == "wender" and attr_str == "act":
                        candidates.append("wender.act")
                    elif attr_str is not None and _is_forbidden_act_module(attr_str):
                        candidates.append(attr_str)

            for dep in dep_candidates:
                dep_file = _resolve_wender_module_file(dep, base_dir, wender_root)
                if dep_file is not None and dep_file not in visited and dep_file not in queue:
                    queue.append(dep_file)

            for cand in candidates:
                if _is_forbidden_act_module(cand):
                    violations.append({
                        "file": rel_name,
                        "line": lineno,
                        "imported": cand,
                    })
                    break

    return violations


# @anchor:wa.act.hosted_guard.assert_plan_has_no_act_imports
def assert_plan_has_no_act_imports(
    plan_dir: str | os.PathLike[str] | None = None,
) -> None:
    """Raise HostedActuationRefused if any file in plan_dir statically imports wender.act."""
    violations = scan_plan_act_imports(plan_dir)
    if violations:
        first = violations[0]
        raise HostedActuationRefused(
            f"planner module {first['file']}:{first['line']} imports forbidden actuation module {first['imported']}",
            code=ACT_IMPORT_REFUSAL_CODE,
            entry="wender.plan",
            reason="static_act_import",
        )


install_executor_guards()
