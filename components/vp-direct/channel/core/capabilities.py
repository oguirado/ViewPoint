"""Effective channel capabilities from the W-L0 receipt and private qualifications."""

from dataclasses import dataclass
import re

from .qualification import load_host_qualifications, qualified_atspi_click


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
    def __init__(self, wl0=None):
        self._qualifications = load_host_qualifications(expected=wl0)
        self._apps = self._qualifications.get("apps", {})
        self._steps = self._qualifications.get("steps", {})

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

        kind = session["kind"]
        app = session["browser"] if kind == "browser" else target.app
        application = self._apps.get(app, {})
        facts = application.get(backend, {})
        if application.get("kind") != kind or facts.get("available") is not True:
            return False
        op = request["op"]
        if backend == "cdp":
            return facts.get("status") == "PASS" and self._cdp_allows(kind, request)
        if backend != "atspi" or facts.get("status") not in {"PASS", "USABLE"}:
            return False
        if op != "act":
            return op in {"observe", "locate", "assert"}

        scoped = qualified_atspi_click(self._qualifications, app)
        action = observation.atspi_action
        return (
            scoped is not None and request["action"] == "click"
            and isinstance(action, AtspiAction) and type(action.index) is int
            and action.index == scoped["action_index"]
            and observation.node_matches == (action.node_id,)
            and (action.target, action.interface, action.method)
            == (scoped.get("target"), scoped.get("interface"), scoped.get("method"))
        )

    def _cdp_allows(self, kind, request):
        op = request["op"]
        if kind == "app":
            # L0-7 proves a reachable endpoint. S7 qualifies observation for these apps.
            return op == "observe" and self._steps["L0-7"]["status"] == "PASS"
        if op == "detach":
            handoff = self._steps["L0-6"]
            return handoff["status"] == "PASS" and handoff["reattach"] == "reconnect"
        ax_probe = self._steps["L0-5"]
        if ax_probe["status"] != "PASS":
            return False
        if op == "act":
            return (request["action"] == "focus" and ax_probe["focus_in_page"] is True
                    and ax_probe["operator_active_window_unchanged"] is True)
        return op in {"attach", "observe", "locate", "assert"}
