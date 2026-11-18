"""Verify the W-L0 evidence used to qualify host channel and frame operations."""

from hashlib import sha256
from pathlib import Path

from bus.broker.qualification import load_wl0
from schemas.validation import canonical_bytes


_WL0_PATH = Path(__file__).resolve().parents[2] / "config" / "wl0.json"
_QUALIFIED_APPS = ("google-chrome", "Antigravity 2", "Codex Desktop")
_STEP_FIELDS = {
    "L0-4": ("status", "backend", "capture_method", "hidden", "non_blank", "frames_differ",
             "storage", "operator_active_window_unchanged", "operator_workspace_unchanged"),
    "L0-5": ("status", "backend", "focus_in_page", "operator_active_window_unchanged"),
    "L0-6": ("status", "reattach"),
    "L0-7": ("status",),
    "L0-8": ("status", "backend", "interface", "method", "action_index", "app", "target",
             "returned", "operator_active_window_unchanged", "operator_workspace_unchanged"),
}
# Canonical digest of the qualification fields in the reviewed repository receipt.
# source.sha256 is part of this projection and identifies the separate live report.
_QUALIFICATION_SHA256 = "a771e3f60945294622966e6fdfc689a35a2e9424787bb3860e11247bc3f1c055"


def _fields(row, names):
    return {name: row[name] for name in names}


def _project(receipt):
    apps = {}
    for app in _QUALIFIED_APPS:
        row = receipt["apps"][app]
        apps[app] = {
            "kind": row["kind"],
            "cdp": _fields(row["cdp"], ("available", "status")),
            "atspi": _fields(row["atspi"], ("available", "status", "non_focusing_action_qualified")),
        }
    return {
        "source": _fields(receipt["source"], ("document", "sha256", "claim_label")),
        "steps": {step: _fields(receipt["steps"][step], fields) for step, fields in _STEP_FIELDS.items()},
        "apps": apps,
    }


def load_host_qualifications(source=None, *, expected=None):
    """Return verified facts, or an empty mapping when host evidence is unavailable.

    The optional expected receipt corroborates the provisioned source. It cannot
    supply qualifications of its own.
    """
    try:
        facts = _project(load_wl0(_WL0_PATH if source is None else source))
        evidence = canonical_bytes(facts)
        if sha256(evidence).hexdigest() != _QUALIFICATION_SHA256:
            return {}
        if expected is not None and canonical_bytes(_project(load_wl0(expected))) != evidence:
            return {}
    except (KeyError, TypeError, ValueError, RecursionError):
        return {}
    return facts


def qualified_atspi_click(qualifications, app):
    """Return the scoped hidden Chrome tab action when the receipt supports it."""
    step = qualifications.get("steps", {}).get("L0-8", {})
    application = qualifications.get("apps", {}).get(app, {})
    facts = application.get("atspi", {})
    if (
        application.get("kind") == "browser" and facts.get("available") is True
        and facts.get("status") in {"PASS", "USABLE"}
        and facts.get("non_focusing_action_qualified") is True
        and step.get("status") == "PASS" and step.get("app") == app
        and step.get("backend") == "atspi" and step.get("interface") == "org.a11y.atspi.Action"
        and step.get("method") == "DoAction" and type(step.get("action_index")) is int
        and step["action_index"] == 0 and step.get("returned") is True
        and step.get("operator_active_window_unchanged") is True
        and step.get("operator_workspace_unchanged") is True
    ):
        return step
    return None
