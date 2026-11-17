"""Effective channel capabilities from the W-L0 receipt and private qualifications."""

from copy import deepcopy
from dataclasses import dataclass
import re


SEMANTIC_BACKENDS = frozenset({"cdp", "atspi"})


@dataclass(frozen=True)
class AtspiAction:
    """Trusted classification of a resolved AX node and its available action."""

    node_id: str
    target: str
    interface: str
    method: str
    index: int


class EffectiveCapabilities:
    def __init__(self, wl0):
        if wl0.get("schema") != "vp.l0_receipt/v1":
            raise ValueError("Expected vp.l0_receipt/v1")
        self._apps = deepcopy(wl0["apps"])
        self._l08 = deepcopy(wl0["steps"]["L0-8"])

    def allows(self, backend, target, request, observation):
        session = target.session
        if backend == "cdp" and (
            session["cdp_allowed"] is not True or not session.get("cdp_port")
            or not session.get("cdp_target_id")
        ):
            return False
        if target.surface_kind == "private":
            return (re.fullmatch(r"xpra:[0-9]+", session["display"]) is not None
                    and backend in target.private_backends)
        if target.surface_kind != "host" or session["display"] != ":0":
            return False

        app = session["browser"] if session["kind"] == "browser" else target.app
        facts = self._apps.get(app, {}).get(backend, {})
        if facts.get("available") is not True:
            return False
        if backend == "cdp":
            return facts.get("status") == "PASS"
        if backend != "atspi" or facts.get("status") not in {"PASS", "USABLE"}:
            return False
        if request["op"] != "act":
            return True

        scoped = self._l08
        action = observation.atspi_action
        return (
            scoped.get("status") == "PASS" and scoped.get("returned") is True
            and scoped.get("operator_active_window_unchanged") is True
            and scoped.get("operator_workspace_unchanged") is True
            and scoped.get("backend") == "atspi" and scoped.get("app") == app
            and facts.get("non_focusing_action_qualified") is True
            and request["action"] == "click" and isinstance(action, AtspiAction)
            and type(action.index) is int and type(scoped.get("action_index")) is int
            and action.index == scoped["action_index"]
            and observation.node_matches == (action.node_id,)
            and (action.target, action.interface, action.method)
            == (scoped.get("target"), scoped.get("interface"), scoped.get("method"))
        )
