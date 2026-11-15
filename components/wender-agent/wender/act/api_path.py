# @module {"id":"wa.act.api_path","layer":1,"owner":"ACT","deps":["wa.state.schema"],"book":"HB-WA-s2-act"}
"""@module wa.act.api_path - app-API action path stub with Blender bpy example adapter."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import re
from typing import Any

from ..state.schema import dumps, StateError

_DOTTED_ID_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+$")


# @anchor:wa.act.api_path.ApiRefused
class ApiRefused(ValueError):
    """Raised when an API action call or specification is refused or invalid."""

    pass


def _validate_params(obj: Any, path: str = "params") -> None:
    if isinstance(obj, float):
        raise ApiRefused("float not allowed at %s" % path)
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return
    if isinstance(obj, int):
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ApiRefused("dict key at %s must be str, got %r" % (path, type(k)))
            _validate_params(v, "%s.%s" % (path, k))
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _validate_params(v, "%s[%d]" % (path, i))
        return
    raise ApiRefused("unsupported type at %s: %r" % (path, type(obj)))


# @anchor:wa.act.api_path.ApiCallSpec
@dataclass(frozen=True)
class ApiCallSpec:
    """Frozen specification of an app API call."""

    program: str
    call: str
    params: dict

    def __post_init__(self) -> None:
        if not isinstance(self.program, str) or not self.program:
            raise ApiRefused("program must be a non-empty string")
        if not isinstance(self.call, str) or not _DOTTED_ID_RE.match(self.call):
            raise ApiRefused("call must be a valid dotted identifier, got %r" % (self.call,))
        if not isinstance(self.params, dict):
            raise ApiRefused("params must be a dict")
        _validate_params(self.params)


# @anchor:wa.act.api_path.AppApiAdapter
class AppApiAdapter(ABC):
    """Abstract base adapter for application API action path."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the application or subsystem target."""
        pass

    @property
    @abstractmethod
    def capabilities(self) -> tuple[str, ...]:
        """Sorted tuple of available API call names."""
        pass

    @abstractmethod
    def call(self, name: str, params: dict) -> dict:
        """Execute or plan an API call, returning result dict."""
        pass


def _render_arg(v: Any) -> str:
    if isinstance(v, dict):
        items = ", ".join("%s: %s" % (repr(k), _render_arg(val)) for k, val in sorted(v.items()))
        return "{%s}" % items
    if isinstance(v, (list, tuple)):
        items = ", ".join(_render_arg(x) for x in v)
        return "[%s]" % items
    return repr(v)


# @anchor:wa.act.api_path.BpyExampleAdapter
class BpyExampleAdapter(AppApiAdapter):
    """Example Blender bpy adapter rendering python snippet strings without importing bpy."""

    ALLOWLIST = (
        "bpy.ops.mesh.primitive_cube_add",
        "bpy.ops.object.delete",
        "bpy.ops.transform.translate",
    )
    IRREVERSIBLE_CALLS = frozenset({"bpy.ops.object.delete"})

    @property
    def name(self) -> str:
        return "blender_bpy"

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self.ALLOWLIST))

    def is_irreversible(self, call_name: str) -> bool:
        return call_name in self.IRREVERSIBLE_CALLS

    def call(self, name: str, params: dict) -> dict:
        if name not in self.ALLOWLIST:
            raise ApiRefused("unknown call %r for adapter %r" % (name, self.name))

        kwargs = ", ".join("%s=%s" % (k, _render_arg(v)) for k, v in sorted(params.items()))
        snippet = f"import bpy\n{name}({kwargs})"
        return {
            "status": "planned",
            "call": name,
            "params": params,
            "snippet": snippet,
        }


# @anchor:wa.act.api_path.FakeApiAdapter
class FakeApiAdapter(AppApiAdapter):
    """Fake API adapter that records calls for tests."""

    def __init__(self, name: str = "fake", allowed: tuple[str, ...] | None = None,
                 irreversible: set[str] | None = None) -> None:
        self._name = name
        self._allowed = tuple(sorted(allowed)) if allowed is not None else (
            "fake.ops.add",
            "fake.ops.delete",
        )
        self._irreversible = set(irreversible) if irreversible is not None else {"fake.ops.delete"}
        self.recorded_calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self._allowed))

    def is_irreversible(self, call_name: str) -> bool:
        return call_name in self._irreversible

    def call(self, name: str, params: dict) -> dict:
        if name not in self._allowed:
            raise ApiRefused("unknown call %r for adapter %r" % (name, self.name))
        record = {"call": name, "params": params}
        self.recorded_calls.append(record)
        return {"status": "ok", "recorded": record}


# @anchor:wa.act.api_path.run_api_action
def run_api_action(adapter: AppApiAdapter, spec: ApiCallSpec, confirm_irreversible: bool = False) -> dict:
    """Validate and dispatch an ApiCallSpec via an AppApiAdapter."""
    if not isinstance(spec, ApiCallSpec):
        raise ApiRefused("spec must be an ApiCallSpec")

    call_name = spec.call
    if call_name not in adapter.capabilities:
        raise ApiRefused("call %r not allowed by adapter %r" % (call_name, adapter.name))

    is_irreversible = False
    if hasattr(adapter, "is_irreversible") and callable(adapter.is_irreversible):
        is_irreversible = adapter.is_irreversible(call_name)

    if is_irreversible and not confirm_irreversible:
        raise ApiRefused("call %r is irreversible and confirm_irreversible is False" % (call_name,))

    res = adapter.call(call_name, spec.params)

    receipt = {
        "adapter": adapter.name,
        "program": spec.program,
        "call": spec.call,
        "params": spec.params,
        "result": res,
    }
    dumps(receipt)
    return receipt
