"""Admission policy for normalized ViewPoint control requests."""

from contextlib import closing
from dataclasses import dataclass, replace
from graphlib import TopologicalSorter
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import time


_SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"
_spec = importlib.util.spec_from_file_location("vp_pace_schema_validation", _SCHEMAS / "validation.py")
_validation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_validation)

_REQUEST = _validation._store()["urn:vp.control.req:v1"]
_CAPABILITY = _validation._store()["urn:vp.capability:v1"]
_RANK = {risk: rank for rank, risk in enumerate(_CAPABILITY["properties"]["risk_class"]["enum"])}
_TOOLS = frozenset(_REQUEST["properties"]["op"]["enum"])
_ADAPTERS = json.loads((_SCHEMAS / "adapter_ops.json").read_text(encoding="utf-8"))
_CHANNEL = next(rule["then"]["properties"]["args"]["properties"] for rule in _REQUEST["allOf"]
                if rule["if"]["properties"]["op"]["const"] == "vp_channel_call")
_SELECTIONS = frozenset({"PRIMARY", "CLIPBOARD"})
_TERMINAL_SELECTION_OPS = {("vp_terminal_call", "copy"), ("vp_terminal_call", "paste")}
# Unrestricted keystrokes can invoke selection shortcuts. Admit an audited
# text-insertion operation separately from paste and generic keystrokes.
_SELECTION_ACTIONS = {"paste", "press", "key"}
_NS_PER_MS = 1_000_000


@dataclass(frozen=True)
class OperationPolicy:
    """Trusted binding review of the operation and all possible delegated calls."""

    risk_class: str
    act: bool
    effect_sensitive: bool
    selections: frozenset[str] = frozenset()
    calls: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class SitePolicy:
    """Authoritative, shared policy resolved for the target site."""

    site_group: str
    min_interval_ms: int | None
    risk_ceiling: str

    def __post_init__(self):
        if (not isinstance(self.site_group, str) or not self.site_group or self.risk_ceiling not in _RANK
                or (self.min_interval_ms is not None and
                    (type(self.min_interval_ms) is not int or self.min_interval_ms < 0))):
            raise ValueError("Invalid site policy")


def _operation_key(tool, args):
    if tool == "vp_channel_call" and args.get("op") == "act":
        return (tool, "act", args.get("action"))
    if tool == "vp_channel_call" or tool in _ADAPTERS:
        return (tool, args.get("op"))
    return (tool,)


def _valid_operation(key):
    tool, *selectors = key
    if tool == "vp_channel_call":
        if not selectors or selectors[0] not in _CHANNEL["op"]["enum"]:
            return False
        if selectors[0] == "act":
            return len(selectors) == 2 and selectors[1] in _CHANNEL["action"]["enum"]
        return len(selectors) == 1
    if tool in _ADAPTERS:
        return len(selectors) == 1 and selectors[0] in _ADAPTERS[tool]["ops"]
    return tool in _TOOLS and not selectors


def _risk_floor(key, declared):
    tool, *selectors = key
    if tool in _ADAPTERS:
        declared = max(declared, _ADAPTERS[tool]["ops"][selectors[0]]["risk_class"], key=_RANK.__getitem__)
    return declared


def _compile_policies(policies):
    for key, policy in policies.items():
        if (not isinstance(key, tuple) or not key or not _valid_operation(key)
                or not isinstance(policy, OperationPolicy) or policy.risk_class not in _RANK
                or type(policy.act) is not bool or type(policy.effect_sensitive) is not bool
                or not policy.selections <= _SELECTIONS):
            raise ValueError(f"Invalid operation policy for {key!r}")
        if any(child not in policies for child in policy.calls):
            raise ValueError(f"Unreviewed delegated operation for {key!r}")
    graph = TopologicalSorter({key: policy.calls for key, policy in policies.items()})
    result = {}
    for key in graph.static_order():
        policy = policies[key]
        children = [result[child] for child in policy.calls]
        risk = max((_risk_floor(key, policy.risk_class), *(child.risk_class for child in children)),
                   key=_RANK.__getitem__)
        channel_act = key[:2] == ("vp_channel_call", "act")
        selections = policy.selections.union(*(child.selections for child in children))
        if key in _TERMINAL_SELECTION_OPS or (channel_act and key[2] in _SELECTION_ACTIONS):
            selections |= _SELECTIONS
        result[key] = replace(policy, risk_class=risk, selections=selections,
                              act=policy.act or channel_act or any(child.act for child in children),
                              effect_sensitive=policy.effect_sensitive or
                              any(child.effect_sensitive for child in children))
    return result


class SitePace:
    """Use the same local database path in each dispatcher process."""

    def __init__(self, path, *, clock=time.monotonic_ns, boot_id=None):
        self._path = Path(path).resolve()
        self._clock = clock
        self._boot_id = boot_id if boot_id is not None else Path(
            "/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if not self._boot_id:
            raise ValueError("Pacing requires a boot identity")
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS last_act (site TEXT PRIMARY KEY, "
                       "boot TEXT NOT NULL, admitted_ns INTEGER NOT NULL)")

    def admit(self, site, *, dry_run):
        interval_ns = site.min_interval_ms * _NS_PER_MS
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = self._clock()
            previous = db.execute("SELECT boot, admitted_ns FROM last_act WHERE site = ?",
                                  (site.site_group,)).fetchone()
            if previous is not None:
                boot, admitted = previous
                # Across boots only current boot age is a proven lower bound.
                elapsed = now - admitted if boot == self._boot_id else now
                if elapsed < interval_ns:
                    return False
            if not dry_run:
                db.execute("INSERT INTO last_act VALUES (?, ?, ?) ON CONFLICT(site) DO UPDATE "
                           "SET boot=excluded.boot, admitted_ns=excluded.admitted_ns",
                           (site.site_group, self._boot_id, now))
            return True


class PaceGate:
    """C1 gate. Inject trusted site resolution and a read-only approval check.

    ``site_for`` resolves the actual target to a SitePolicy or None.
    ``has_approval`` checks a current human grant for (site, risk, request digest).
    The digest covers the normalized caller, target, epoch and complete payload.
    """

    def __init__(self, policies, *, site_for, has_approval, pace):
        self._policies = _compile_policies(dict(policies))
        self._site_for = site_for
        self._has_approval = has_approval
        self._pace = pace

    def __call__(self, request, registry_risk):
        tool, args, context = request["op"], request["args"], request["context"]
        key = _operation_key(tool, args)
        policy = self._policies.get(key)
        if policy is None:
            return "HOLD"
        if context["caller"] == "agent" and policy.selections:
            return "NO_LEASE"

        risks = [policy.risk_class, registry_risk]
        if tool == "vp_channel_call":
            risks.append(args["risk_class"])
        if any(risk not in _RANK for risk in risks):
            return "HOLD"
        risk = max(risks, key=_RANK.__getitem__)
        rank = _RANK[risk]
        site = self._site_for(request)
        if (site is None or site.min_interval_ms is None) and rank > _RANK["leer"]:
            return "PACE_LIMITED"
        if (site is None and policy.act) or (site is not None and rank > _RANK[site.risk_ceiling]):
            return "NO_LEASE"
        if policy.effect_sensitive or rank >= _RANK["enviar"]:
            digest = hashlib.sha256(_validation.canonical_bytes(request)).hexdigest()
            if site is None or self._has_approval(site.site_group, risk, digest) is not True:
                return "HUMAN_NEEDED"
        if policy.act and site.min_interval_ms is not None:
            try:
                if not self._pace.admit(site, dry_run=request["dry_run"]):
                    return "PACE_LIMITED"
            except sqlite3.Error:
                return "HOLD"
        return True
