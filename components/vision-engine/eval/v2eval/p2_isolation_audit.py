"""P2/VC offline process-isolation audit and source-pinned mutation ledger.

Only the public `run_originals` and `run_orders` entry points execute tests:
they run *real* unittest TestCase methods (with fixtures and cleanups) in
bounded child processes. A detected process-state leak is not a semantic kill.
This module never imports the known poison test or repairs its globals.
"""
from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass, fields
import gc
import hashlib
import importlib
import json
import linecache
import multiprocessing
import os
import random
import re
import signal
import sys
import threading
import time
import traceback
import types
import unittest
import weakref
import _thread
from typing import Iterable, Mapping, Sequence


AST_SITE_SCHEMA = "p2.ast_site/v1"
MUTATION_TRIAL_SCHEMA = "p2.mutation_trial/v1"
CATEGORIES = ("OPERATOR_FLIP", "BOUNDARY_OFF_BY_ONE", "DROPPED_CHECK", "SWAPPED_ARGS")
DISPOSITIONS = (
    "KILLED", "SURVIVED", "EQUIVALENT", "INVALID_MUTANT", "INFRASTRUCTURE",
    "BASELINE_RED", "ANCHOR_STALE", "DEFERRED_OWNER",
)
COUNTS = (
    "raw_sites", "eligible_sites", "selected", "not_selected", "generated",
    "invalid", "executed", "killed", "survived", "equivalent",
    "infrastructure", "baseline_red", "stale", "deferred",
)
AST_SITE_FIELDS = (
    "schema", "module_path", "source_sha256", "qualified_ast_scope", "containing_expression",
    "original_expression", "structural_selector", "transformation", "site_id",
)
MUTATION_TRIAL_FIELDS = (
    "schema", "campaign_id", "site", "fixture_sha256", "test_ids", "seed",
    "control_outcome", "mutant_outcome", "assertion_identity", "observed_effect_digest",
    "elapsed_ns", "disposition", "equivalence_proof_ref",
)
MAX_SPECIMEN_SECONDS = 60
MAX_SPECIMEN_BYTES = 2 * 1024**3
MAX_CATEGORY_SELECTION = 8
_ISOLATION = "ISOLATION_FAILURE"
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SELECTOR = re.compile(r"(?:[a-zA-Z_][a-zA-Z_0-9]*(?:\[(?:\d+|\*)\])?)(?:\.(?:[a-zA-Z_][a-zA-Z_0-9]*(?:\[(?:\d+|\*)\])?))*\Z")
_SELECTOR_PART = re.compile(r"([a-zA-Z_][a-zA-Z_0-9]*)(?:\[(\d+|\*)\])?\Z")


class AuditRefusal(ValueError):
    """Invalid contract input rejected without spawning a worker or disk effects."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuditRefusal("E_AUDIT_SCHEMA", f"{name} must be nonempty text")
    return value


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise AuditRefusal("E_AUDIT_DIGEST", f"{name} must be a lowercase SHA-256")
    return value


def sha256_bytes(data: bytes) -> str:
    if not isinstance(data, bytes):
        raise AuditRefusal("E_AUDIT_SOURCE", "source must be exact immutable bytes")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class AstSite:
    schema: str
    module_path: str
    source_sha256: str
    qualified_ast_scope: str
    containing_expression: str
    original_expression: str
    structural_selector: str
    transformation: str
    site_id: str

    def __post_init__(self) -> None:
        if self.schema != AST_SITE_SCHEMA:
            raise AuditRefusal("E_AUDIT_SCHEMA", "ast site schema")
        for name in ("module_path", "qualified_ast_scope", "containing_expression", "original_expression", "site_id"):
            _nonempty(getattr(self, name), name)
        if self.module_path.startswith("/") or ".." in self.module_path.split("/") or not self.module_path.endswith(".py"):
            raise AuditRefusal("E_AUDIT_SCHEMA", "unsafe module path")
        _sha(self.source_sha256, "source_sha256")
        if (not isinstance(self.structural_selector, str) or
                _SELECTOR.fullmatch(self.structural_selector) is None or
                self.structural_selector.isdigit()):
            raise AuditRefusal("ANCHOR_STALE", "selector must identify AST fields, not line/ordinal")
        if self.transformation not in CATEGORIES and self.transformation not in (
            "BOUNDARY_OFF_BY_ONE:+1", "BOUNDARY_OFF_BY_ONE:-1",
        ):
            raise AuditRefusal("E_AUDIT_SCHEMA", "unsupported transformation")

    def to_dict(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class MutationTrial:
    schema: str
    campaign_id: str
    site: AstSite
    fixture_sha256: str
    test_ids: tuple[str, ...]
    seed: int
    control_outcome: str
    mutant_outcome: str
    assertion_identity: str
    observed_effect_digest: str
    elapsed_ns: int
    disposition: str
    equivalence_proof_ref: str | None

    def __post_init__(self) -> None:
        if self.schema != MUTATION_TRIAL_SCHEMA or not isinstance(self.site, AstSite):
            raise AuditRefusal("E_AUDIT_SCHEMA", "mutation trial schema/site")
        _nonempty(self.campaign_id, "campaign_id")
        _sha(self.fixture_sha256, "fixture_sha256")
        _sha(self.observed_effect_digest, "observed_effect_digest")
        _check_test_ids(self.test_ids)
        _nonempty(self.assertion_identity, "assertion_identity")
        if type(self.seed) is not int or type(self.elapsed_ns) is not int or self.elapsed_ns < 0:
            raise AuditRefusal("E_AUDIT_SCHEMA", "seed/elapsed must be integers")
        if self.disposition not in DISPOSITIONS:
            raise AuditRefusal("E_AUDIT_SCHEMA", "invalid disposition")
        if self.disposition == "EQUIVALENT" and not (
            isinstance(self.equivalence_proof_ref, str)
            and self.equivalence_proof_ref.startswith("reviewed:")
            and self.site.source_sha256 in self.equivalence_proof_ref
        ):
            raise AuditRefusal("E_AUDIT_PROOF", "equivalence requires reviewed current-source proof")

    def to_dict(self) -> dict[str, object]:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["site"] = self.site.to_dict()
        d["test_ids"] = list(self.test_ids)
        return d


@dataclass(frozen=True)
class FrozenPopulation:
    module_path: str
    category: str
    raw_sites: int
    eligible_sites: tuple[AstSite, ...]
    selected_sites: tuple[AstSite, ...]
    not_selected: int


@dataclass(frozen=True)
class FrozenCampaign:
    campaign_id: str
    fixture_sha256: str
    source_digests: Mapping[str, str]
    populations: tuple[FrozenPopulation, ...]
    seed: int


@dataclass(frozen=True)
class AuditRun:
    requested: tuple[str, ...]
    executed: tuple[str, ...]
    status: str
    cases: tuple[tuple[str, str], ...]
    failures: tuple[tuple[str, str, bool], ...]  # id, exception type, method-body traceback?
    errors: tuple[tuple[str, str], ...]
    skipped: tuple[str, ...]
    expected_failures: tuple[str, ...]
    unexpected_successes: tuple[str, ...]
    leaks: Mapping[str, int]
    restored: bool
    elapsed_ns: int
    infrastructure_detail: str = ""

    @property
    def isolation_failed(self) -> bool:
        return self.status == _ISOLATION

    @property
    def clean_pass(self) -> bool:
        return self.status == "PASS" and not self.leaks and self.requested == self.executed


@dataclass(frozen=True)
class OrderComparison:
    solo: tuple[AuditRun, ...]
    coupled: tuple[AuditRun, ...]
    orders: tuple[tuple[str, ...], ...]
    agreement: bool


# AST source pins and structural selectors: paths are from a named AST scope,
# never absolute line numbers. Wildcards are only accepted if matching one node.
def _scopes(tree: ast.AST) -> dict[str, ast.AST]:
    result = {"<module>": tree}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = (prefix + "." if prefix else "") + child.name
                result[name] = child
                visit(child, name)
            else:
                visit(child, prefix)

    visit(tree, "")
    return result


def _paths(root: ast.AST):
    def visit(node: ast.AST, path: str, current_container: ast.expr | None):
        if isinstance(node, ast.expr) and current_container is None:
            current_container = node
        yield (path, node, current_container)
        for fld, val in ast.iter_fields(node):
            if isinstance(val, ast.AST):
                yield from visit(val, (path + "." if path else "") + fld, current_container)
            elif isinstance(val, list):
                for idx, item in enumerate(val):
                    if isinstance(item, ast.AST):
                        yield from visit(item, (path + "." if path else "") + f"{fld}[{idx}]", current_container)
    yield from visit(root, "", None)


def _pattern(selector: str) -> re.Pattern[str]:
    if _SELECTOR.fullmatch(selector) is None:
        raise AuditRefusal("ANCHOR_STALE", "invalid/bare ordinal structural selector")
    chunks = []
    for component in selector.split("."):
        match = _SELECTOR_PART.fullmatch(component)
        if match is None:
            raise AuditRefusal("ANCHOR_STALE", "invalid selector field")
        field, idx = match.groups()
        chunks.append(re.escape(field) + (r"\[\d+\]" if idx == "*" else re.escape(f"[{idx}]") if idx is not None else ""))
    return re.compile("^" + r"\.".join(chunks) + "$")


def _resolve(tree: ast.AST, site: AstSite) -> ast.expr:
    root = _scopes(tree).get(site.qualified_ast_scope)
    if root is None:
        raise AuditRefusal("ANCHOR_STALE", "named AST scope disappeared")
    pat = _pattern(site.structural_selector)
    matches = [node for path, node, container in _paths(root)
               if path and pat.fullmatch(path) and isinstance(node, ast.expr)
               and ast.unparse(node) == site.original_expression
               and container is not None and ast.unparse(container) == site.containing_expression]
    if len(matches) != 1:
        raise AuditRefusal("ANCHOR_STALE", f"matched {len(matches)} structural expressions, require exactly one")
    return matches[0]


def _operator_flip(node: ast.expr) -> bool:
    operators = {
        ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
        ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
        ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv,
        ast.And: ast.Or, ast.Or: ast.And,
    }
    if isinstance(node, ast.BoolOp):
        node.op = operators[type(node.op)]() if type(node.op) in operators else node.op
        return type(node.op) in (ast.And, ast.Or)
    if isinstance(node, ast.BinOp) and type(node.op) in operators:
        node.op = operators[type(node.op)]()
        return True
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in operators:
        node.ops[0] = operators[type(node.ops[0])]()
        return True
    return False


def _eligible(node: ast.expr, category: str, path: str) -> bool:
    if category == "OPERATOR_FLIP":
        return (isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or))) or (
            isinstance(node, ast.BinOp) and type(node.op) in (ast.Add, ast.Sub, ast.Mult)) or (
            isinstance(node, ast.Compare) and len(node.ops) == 1
            and type(node.ops[0]) in (ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.Lt, ast.LtE, ast.Gt, ast.GtE))
    if category == "BOUNDARY_OFF_BY_ONE":
        return isinstance(node, ast.Constant) and type(node.value) is int
    if category == "DROPPED_CHECK":
        return path.endswith(".test") and not (isinstance(node, ast.Constant) and node.value is False)
    if category == "SWAPPED_ARGS":
        return isinstance(node, ast.Call) and len(node.args) >= 2
    return False


def mutate_source(source: bytes, site: AstSite) -> bytes:
    """Return mutated UTF-8 source with exactly one expression replaced.

    `ANCHOR_STALE` is raised for a changed digest, missing/ambiguous structural
    selector or mismatching original/container expression. No ordinal fallback.
    """
    if sha256_bytes(source) != site.source_sha256:
        raise AuditRefusal("ANCHOR_STALE", "pinned source digest changed")
    try:
        tree = ast.parse(source.decode("utf-8"), filename=site.module_path)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise AuditRefusal("E_AUDIT_SOURCE", "unparseable pinned source") from exc
    node = _resolve(tree, site)
    category = site.transformation.split(":", 1)[0]
    if not _eligible(node, category, site.structural_selector):
        raise AuditRefusal("E_AUDIT_MUTANT", "not an eligible expression")
    if category == "OPERATOR_FLIP":
        changed = _operator_flip(node)
    elif category == "BOUNDARY_OFF_BY_ONE":
        delta = -1 if site.transformation.endswith(":-1") else 1
        node.value += delta
        changed = True
    elif category == "DROPPED_CHECK":
        class _OneExpression(ast.NodeTransformer):
            def visit(self, candidate):
                if candidate is node:
                    return ast.copy_location(ast.Constant(value=False), candidate)
                return super().visit(candidate)
        tree = _OneExpression().visit(tree)
        changed = True
    else:  # SWAPPED_ARGS
        node.args[0], node.args[1] = node.args[1], node.args[0]
        changed = True
    if not changed:
        raise AuditRefusal("E_AUDIT_MUTANT", "no valid single expression mutation")
    ast.fix_missing_locations(tree)
    try:
        mutated = ast.unparse(tree) + "\n"
        compile(mutated, site.module_path, "exec")
    except (SyntaxError, ValueError, TypeError) as exc:
        raise AuditRefusal("E_AUDIT_MUTANT", "generated mutant does not compile") from exc
    return mutated.encode("utf-8")


def enumerate_sites(module_path: str, source: bytes) -> dict[str, tuple[AstSite, ...]]:
    """Deterministically enumerate all eligible AST expression sites per category."""
    digest = sha256_bytes(source)
    try:
        tree = ast.parse(source.decode("utf-8"), filename=module_path)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise AuditRefusal("E_AUDIT_SOURCE", "unparseable source") from exc
    results: dict[str, list[AstSite]] = {category: [] for category in CATEGORIES}
    for scope, root in sorted(_scopes(tree).items()):
        # Each expression belongs to its *innermost* declared function/class scope.
        for path, node, container in _paths(root):
            if not path or not isinstance(node, ast.expr) or container is None:
                continue
            if isinstance(node, (ast.Lambda, ast.JoinedStr)):
                continue
            for cat in CATEGORIES:
                if not _eligible(node, cat, path):
                    continue
                material = (module_path, digest, scope, ast.unparse(container),
                            ast.unparse(node), path, cat)
                site_id = hashlib.sha256(json.dumps(material, separators=(",", ":"),
                                                    ensure_ascii=True).encode("utf-8")).hexdigest()
                results[cat].append(AstSite(AST_SITE_SCHEMA, module_path, digest, scope,
                                            ast.unparse(container), ast.unparse(node), path,
                                            cat, site_id))
    # A node can appear in the top-level traversal and a named function's traversal;
    # prefer the most qualified scope, never double-count that same source location.
    for cat in CATEGORIES:
        by_position: dict[tuple[int, int], AstSite] = {}
        for s in results[cat]:
            node = _resolve(tree, s)
            position = (node.lineno, node.col_offset)
            if position not in by_position or (
                by_position[position].qualified_ast_scope == "<module>"
                and s.qualified_ast_scope != "<module>"
            ) or (
                s.qualified_ast_scope != "<module>"
                and by_position[position].qualified_ast_scope != "<module>"
                and s.qualified_ast_scope.count(".") > by_position[position].qualified_ast_scope.count(".")
            ):
                by_position[position] = s
        results[cat] = sorted(by_position.values(), key=lambda s: (s.qualified_ast_scope, s.structural_selector))
    return {cat: tuple(results[cat]) for cat in CATEGORIES}



def _raw_site_counts(source: bytes, module_path: str) -> dict[str, int]:
    """Count raw syntactic candidate expressions, before semantic eligibility.

    Eligibility excludes strings from integer boundaries, zero/one-argument
    calls from argument swaps, and unsupported operators. Raw counts therefore
    need not equal eligible counts and may never be reduced by missing receipts.
    """
    tree = ast.parse(source.decode("utf-8"), filename=module_path)
    raw: dict[str, set[tuple[int, int]]] = {category: set() for category in CATEGORIES}
    for path, node, _container in _paths(tree):
        if not path or not isinstance(node, ast.expr):
            continue
        position = (node.lineno, node.col_offset)
        if isinstance(node, (ast.BoolOp, ast.BinOp, ast.Compare)):
            raw["OPERATOR_FLIP"].add(position)
        if isinstance(node, ast.Constant):
            raw["BOUNDARY_OFF_BY_ONE"].add(position)
        if path.endswith(".test"):
            raw["DROPPED_CHECK"].add(position)
        if isinstance(node, ast.Call):
            raw["SWAPPED_ARGS"].add(position)
    return {category: len(positions) for category, positions in raw.items()}


def freeze_campaign(campaign_id: str, sources: Mapping[str, bytes], fixture: bytes,
                    *, seed: int = 17, max_per_category: int = MAX_CATEGORY_SELECTION) -> FrozenCampaign:
    """Freeze source/fixture digests and every eligible/selected/unselected site."""
    _nonempty(campaign_id, "campaign_id")
    if not isinstance(fixture, bytes) or not fixture:
        raise AuditRefusal("E_AUDIT_FIXTURE", "fixture must be nonempty immutable bytes")
    if not isinstance(sources, Mapping) or not sources:
        raise AuditRefusal("E_AUDIT_SOURCE", "sources must be a nonempty path->bytes map")
    if type(seed) is not int or type(max_per_category) is not int or not 1 <= max_per_category <= 8:
        raise AuditRefusal("E_AUDIT_BUDGET", "seed integer and category selection 1..8 required")
    populations: list[FrozenPopulation] = []
    digests: dict[str, str] = {}
    for path, raw in sorted(sources.items()):
        if not isinstance(path, str) or not path.endswith(".py") or path.startswith("/") or ".." in path.split("/"):
            raise AuditRefusal("E_AUDIT_SOURCE", "module path must be relative .py")
        digests[path] = sha256_bytes(raw)
        by_cat = enumerate_sites(path, raw)
        raw_counts = _raw_site_counts(raw, path)
        for cat in CATEGORIES:
            eligible = by_cat[cat]
            selected = eligible[:max_per_category]
            populations.append(FrozenPopulation(path, cat, raw_counts[cat], eligible,
                                                 selected, len(eligible) - len(selected)))
    return FrozenCampaign(campaign_id, sha256_bytes(fixture), digests,
                          tuple(populations), seed)


def reduce_campaign(campaign: FrozenCampaign, receipts: Iterable[MutationTrial]) -> dict[str, object]:
    """Reject missing/extra/duplicate receipts; reconcile frozen denominators."""
    if not isinstance(campaign, FrozenCampaign):
        raise AuditRefusal("E_AUDIT_SCHEMA", "campaign must be frozen")
    expected = {s.site_id: (p.module_path, p.category, s)
                for p in campaign.populations for s in p.selected_sites}
    got: dict[str, MutationTrial] = {}
    for r in receipts:
        if not isinstance(r, MutationTrial) or r.campaign_id != campaign.campaign_id:
            raise AuditRefusal("E_AUDIT_RECEIPT", "foreign/invalid trial")
        if (r.fixture_sha256 != campaign.fixture_sha256 or
                r.site.source_sha256 != campaign.source_digests.get(r.site.module_path) or
                r.site.site_id not in expected or r.site != expected[r.site.site_id][2] or
                r.seed != campaign.seed or r.site.site_id in got):
            raise AuditRefusal("E_AUDIT_RECEIPT", "unbound or duplicate site receipt")
        got[r.site.site_id] = r
    if set(got) != set(expected):
        raise AuditRefusal("E_AUDIT_RECEIPT", f"missing {len(set(expected)-set(got))} frozen selected receipts")
    buckets: dict[str, dict[str, int]] = {}
    totals = {name: 0 for name in COUNTS}
    for p in campaign.populations:
        cnt = {name: 0 for name in COUNTS}
        cnt.update(raw_sites=p.raw_sites, eligible_sites=len(p.eligible_sites),
                   selected=len(p.selected_sites), not_selected=p.not_selected)
        for s in p.selected_sites:
            disposition = got[s.site_id].disposition
            key = {
                "INVALID_MUTANT": "invalid", "ANCHOR_STALE": "stale", "DEFERRED_OWNER": "deferred",
                "KILLED": "killed", "SURVIVED": "survived", "EQUIVALENT": "equivalent",
                "INFRASTRUCTURE": "infrastructure", "BASELINE_RED": "baseline_red",
            }[disposition]
            cnt[key] += 1
        cnt["generated"] = cnt["selected"] - cnt["stale"] - cnt["deferred"]
        cnt["executed"] = cnt["generated"] - cnt["invalid"]
        if (cnt["selected"] != cnt["generated"] + cnt["stale"] + cnt["deferred"] or
                cnt["generated"] != cnt["invalid"] + cnt["executed"] or
                cnt["executed"] != sum(cnt[k] for k in (
                    "killed", "survived", "equivalent", "infrastructure", "baseline_red")) or
                cnt["eligible_sites"] != cnt["selected"] + cnt["not_selected"] or
                cnt["raw_sites"] < cnt["eligible_sites"]):
            raise AuditRefusal("E_AUDIT_RECONCILIATION", "mutation population mismatch")
        buckets[f"{p.module_path}|{p.category}"] = cnt
        for name in COUNTS:
            totals[name] += cnt[name]
    return {
        "by_module_category": buckets,
        "totals": totals,
        "gross_score": totals["killed"] / totals["generated"] if totals["generated"] else None,
        "semantic_score": totals["killed"] / (totals["killed"] + totals["survived"])
        if totals["killed"] + totals["survived"] else None,
    }


# Process-state evidence is captured *within* the child after all test modules
# have imported and before calling TestCase.run, then compared before cleanup.
# No source outside this worker's allowed paths is patched or repaired on disk.
def _lock_states() -> dict[int, bool]:
    return {id(obj): obj.locked() for obj in gc.get_objects() if isinstance(obj, _thread.LockType)}


def _weakref_ids() -> frozenset[int]:
    return frozenset(id(obj) for obj in gc.get_objects() if isinstance(obj, weakref.ReferenceType))


def _shm_names() -> frozenset[str]:
    try:
        return frozenset(os.listdir("/dev/shm"))
    except (OSError, AttributeError):
        return frozenset()


def _signal_handlers() -> dict[int, object]:
    result = {}
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGUSR1", signal.SIGTERM)):
        try:
            result[sig] = signal.getsignal(sig)
        except (ValueError, OSError):
            pass
    return result


class _Snapshot:
    def __init__(self) -> None:
        gc.collect()
        self.open = os.open
        self.builtin_open = builtins.open
        self.environ = dict(os.environ)
        self.modules = dict(sys.modules)
        self.globals = {name: dict(mod.__dict__) for name, mod in self.modules.items()
                        if isinstance(mod, types.ModuleType) and
                        (name.startswith("vadapt.") or name.startswith("tests.") or name.startswith("eval."))}
        self.path = tuple(sys.path)
        self.meta_path = tuple(sys.meta_path)
        self.path_hooks = tuple(sys.path_hooks)
        self.path_importer_cache = dict(sys.path_importer_cache)
        self.linecache = dict(linecache.cache)
        self.weakrefs = _weakref_ids()
        self.finalizers = frozenset(weakref.finalize._registry)
        self.lock_states = _lock_states()
        self.threads = frozenset(id(th) for th in threading.enumerate())
        self.shm = _shm_names()
        self.signals = _signal_handlers()

    def compare(self) -> dict[str, int]:
        gc.collect()
        count: dict[str, int] = {}
        if os.open is not self.open:
            count["os.open"] = 1
        if builtins.open is not self.builtin_open:
            count["builtins.open"] = 1
        changed_env = set(os.environ) ^ set(self.environ)
        changed_env.update(k for k in set(os.environ) & set(self.environ)
                           if os.environ[k] != self.environ[k])
        if changed_env:
            count["environment"] = len(changed_env)
        modkeys = set(sys.modules) | set(self.modules)
        modules_changed = sum(sys.modules.get(k, _MISSING) is not self.modules.get(k, _MISSING)
                              for k in modkeys)
        if modules_changed:
            count["module_cache"] = modules_changed
        g_changed = 0
        for name, saved in self.globals.items():
            mod = self.modules[name]
            now = mod.__dict__
            g_changed += sum(now.get(k, _MISSING) is not saved.get(k, _MISSING)
                             for k in set(now) | set(saved))
        if g_changed:
            count["module_globals"] = g_changed
        cache_changes = sum(sys.path_importer_cache.get(k, _MISSING) is not self.path_importer_cache.get(k, _MISSING)
                            for k in set(sys.path_importer_cache) | set(self.path_importer_cache))
        # unittest formats failures using linecache itself; diagnostic output is
        # instrumentation, not a leak by the original test body.
        if (tuple(sys.path) != self.path or tuple(sys.meta_path) != self.meta_path or
                tuple(sys.path_hooks) != self.path_hooks):
            cache_changes += 1
        if cache_changes:
            count["import_caches"] = cache_changes
        new_refs = _weakref_ids() - self.weakrefs
        new_finalizers = frozenset(weakref.finalize._registry) - self.finalizers
        if new_refs or new_finalizers:
            count["weak_references"] = len(new_refs) + len(new_finalizers)
        locks = _lock_states()
        lock_drift = sum(state and (key not in self.lock_states or not self.lock_states[key])
                         for key, state in locks.items())
        if lock_drift:
            count["locks"] = lock_drift
        extra_threads = set(id(th) for th in threading.enumerate()) - self.threads
        if extra_threads:
            count["threads"] = len(extra_threads)
        extra_shm = _shm_names() - self.shm
        if extra_shm:
            count["shm_ownership"] = len(extra_shm)
        if _signal_handlers() != self.signals:
            count["signals"] = 1
        return count

    def restore(self) -> None:
        # These are the execution-boundary substitutions we can safely revert.
        # Threads, externally held locks and SHM are reported, not force-killed.
        try:
            os.open = self.open
            builtins.open = self.builtin_open
            os.environ.clear()
            os.environ.update(self.environ)
            sys.modules.clear()
            sys.modules.update(self.modules)
            for name, values in self.globals.items():
                mod = self.modules[name]
                mod.__dict__.clear()
                mod.__dict__.update(values)
            sys.path[:] = self.path
            sys.meta_path[:] = self.meta_path
            sys.path_hooks[:] = self.path_hooks
            sys.path_importer_cache.clear()
            sys.path_importer_cache.update(self.path_importer_cache)
            linecache.cache.clear()
            linecache.cache.update(self.linecache)
            for sig, handler in self.signals.items():
                signal.signal(sig, handler)
        finally:
            os.open = self.open
            builtins.open = self.builtin_open


_MISSING = object()


class _Capture(unittest.TestResult):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.cases: list[tuple[str, str]] = []
        self.fail_details: list[tuple[str, str, bool]] = []
        self.error_details: list[tuple[str, str]] = []

    def startTest(self, test: unittest.case.TestCase) -> None:
        super().startTest(test)
        self.ids.append(test.id())

    def addSuccess(self, test: unittest.case.TestCase) -> None:
        super().addSuccess(test)
        self.cases.append((test.id(), "PASS"))

    def addFailure(self, test: unittest.case.TestCase, err: tuple) -> None:
        super().addFailure(test, err)
        tb = err[2]
        body_frame = False
        while tb is not None:
            if tb.tb_frame.f_code.co_name == test._testMethodName:
                body_frame = True
                break
            tb = tb.tb_next
        self.fail_details.append((test.id(), err[0].__name__, body_frame))
        self.cases.append((test.id(), "ASSERTION_FAILURE"))

    def addError(self, test: unittest.case.TestCase, err: tuple) -> None:
        super().addError(test, err)
        self.error_details.append((test.id(), err[0].__name__))
        self.cases.append((test.id(), "ERROR"))

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self.cases.append((test.id(), "SKIPPED"))

    def addExpectedFailure(self, test: unittest.case.TestCase, err: tuple) -> None:
        super().addExpectedFailure(test, err)
        self.cases.append((test.id(), "XFAIL"))

    def addUnexpectedSuccess(self, test: unittest.case.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self.cases.append((test.id(), "XPASS"))


def _check_test_ids(test_ids: Sequence[str]) -> tuple[str, ...]:
    if (not isinstance(test_ids, (tuple, list)) or not test_ids or
            any(not isinstance(i, str) or not i.strip() or i.count(".") < 2 for i in test_ids) or
            len(set(test_ids)) != len(test_ids)):
        raise AuditRefusal("E_AUDIT_TESTS", "nonempty, unique fully qualified method IDs required")
    return tuple(test_ids)


def _check_budget(time_limit_s: float, memory_limit_bytes: int) -> None:
    if (type(time_limit_s) not in (float, int) or not 0 < time_limit_s <= MAX_SPECIMEN_SECONDS or
            type(memory_limit_bytes) is not int or not 0 < memory_limit_bytes <= MAX_SPECIMEN_BYTES):
        raise AuditRefusal("E_AUDIT_BUDGET", "at most 60 seconds and 2 GiB per specimen")


def _load_suite(ids: tuple[str, ...]) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in ids:
        item = loader.loadTestsFromName(name)
        leaf = list(_flatten(item))
        if len(leaf) != 1 or leaf[0].id() != name or leaf[0].__class__.__name__ == "_FailedTest":
            raise AuditRefusal("E_AUDIT_TESTS", f"unresolved/non-method test ID: {name}")
        suite.addTests(leaf)
    return suite


def _flatten(suite):
    for x in suite:
        if isinstance(x, unittest.TestSuite):
            yield from _flatten(x)
        elif x is not None:
            yield x


def _inject_mutant(module_path: str, mutated_source: bytes) -> None:
    module_name = module_path.removesuffix(".py").replace("/", ".")
    if "." not in module_name:
        raise AuditRefusal("E_AUDIT_SOURCE", "module path must be inside a package")
    package_name, leaf = module_name.rsplit(".", 1)
    package = importlib.import_module(package_name)
    module = types.ModuleType(module_name)
    module.__package__ = package_name
    module.__file__ = module_path
    sys.modules[module_name] = module
    code = compile(mutated_source, module_path, "exec")
    exec(code, module.__dict__)
    setattr(package, leaf, module)


def _execute_inner(ids: tuple[str, ...], override: tuple[str, bytes] | None = None) -> AuditRun:
    started = time.monotonic_ns()
    snap = None
    capture = _Capture()
    leaks: dict[str, int] = {}
    restored = False
    detail = ""
    status = "INFRASTRUCTURE"
    try:
        if override is not None:
            _inject_mutant(*override)
        suite = _load_suite(ids)
        snap = _Snapshot()
        try:
            suite.run(capture)
            if capture.unexpectedSuccesses:
                status = "XPASS"
            elif capture.skipped:
                status = "SKIPPED"
            elif capture.expectedFailures:
                status = "XFAIL"
            elif capture.errors:
                status = "ERROR"
            elif capture.failures:
                status = "ASSERTION_FAILURE"
            elif capture.testsRun != len(ids) or tuple(capture.ids) != ids:
                status = "INFRASTRUCTURE"
                detail = "test execution IDs differ from pinned IDs"
            else:
                status = "PASS"
        finally:
            # Even a runner exception must compare and restore before exiting.
            try:
                leaks = snap.compare()
            finally:
                snap.restore()
                restored = (os.open is snap.open and builtins.open is snap.builtin_open and
                            dict(os.environ) == snap.environ)
    except BaseException as exc:  # child must preserve infra, not synthesize a kill
        detail = f"{type(exc).__name__}: {str(exc)[:150]}"
        status = "INFRASTRUCTURE"
    if leaks:
        status = _ISOLATION  # independent of inner ASSERTION_FAILURE/ERROR
    return AuditRun(
        ids, tuple(capture.ids), status, tuple(capture.cases), tuple(capture.fail_details),
        tuple(capture.error_details), tuple(test.id() for test, _ in capture.skipped),
        tuple(test.id() for test, _ in capture.expectedFailures),
        tuple(test.id() for test in capture.unexpectedSuccesses),
        leaks, restored, time.monotonic_ns() - started, detail,
    )


def _limit_memory(max_bytes: int) -> None:
    import resource  # Linux VM and CI; other platforms report INFRASTRUCTURE
    old_soft, old_hard = resource.getrlimit(resource.RLIMIT_AS)
    if old_soft != resource.RLIM_INFINITY:
        max_bytes = min(max_bytes, old_soft)
    resource.setrlimit(resource.RLIMIT_AS, (max_bytes, old_hard))


def _worker(pipe, ids: tuple[str, ...], memory_limit_bytes: int,
            override: tuple[str, bytes] | None) -> None:
    try:
        _limit_memory(memory_limit_bytes)
        result = _execute_inner(ids, override)
        pipe.send(result)
    except BaseException as exc:
        pipe.send(AuditRun(ids, (), "INFRASTRUCTURE", (), (), (), (), (), (), {}, False,
                           0, f"worker {type(exc).__name__}: {str(exc)[:150]}"))
    finally:
        pipe.close()


def run_originals(test_ids: Sequence[str], *, time_limit_s: float = MAX_SPECIMEN_SECONDS,
                  memory_limit_bytes: int = MAX_SPECIMEN_BYTES,
                  _override: tuple[str, bytes] | None = None) -> AuditRun:
    """Run complete original/mutant unittest methods in a fresh 60s/2GiB child.

    Uses spawn, not fork, to keep every specimen's imports and os.open identity
    independent. A timeout or worker crash is INFRASTRUCTURE, never KILLED.
    """
    ids = _check_test_ids(test_ids)
    _check_budget(time_limit_s, memory_limit_bytes)
    if _override is not None and (not isinstance(_override, tuple) or len(_override) != 2 or
                                  not isinstance(_override[0], str) or
                                  not isinstance(_override[1], bytes)):
        raise AuditRefusal("E_AUDIT_SOURCE", "invalid pinned mutant")
    started = time.monotonic_ns()
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    worker = context.Process(target=_worker, args=(writer, ids, memory_limit_bytes, _override))
    try:
        worker.start()
        writer.close()
        worker.join(time_limit_s)
        if worker.is_alive():
            worker.terminate()
            worker.join()
            return AuditRun(ids, (), "INFRASTRUCTURE", (), (), (), (), (), (), {},
                            False, time.monotonic_ns() - started, "TIMEOUT")
        try:
            if reader.poll(0):
                result = reader.recv()
                if isinstance(result, AuditRun):
                    return result
        except (EOFError, OSError, ValueError) as exc:
            return AuditRun(ids, (), "INFRASTRUCTURE", (), (), (), (), (), (), {},
                            False, time.monotonic_ns() - started, type(exc).__name__)
        return AuditRun(ids, (), "INFRASTRUCTURE", (), (), (), (), (), (), {},
                        False, time.monotonic_ns() - started, f"worker exit code {worker.exitcode}")
    finally:
        reader.close()
        writer.close()
        if worker.pid is not None and worker.is_alive():
            worker.terminate()
            worker.join()


def run_orders(test_ids: Sequence[str], *, seed: int = 17,
               time_limit_s: float = MAX_SPECIMEN_SECONDS,
               memory_limit_bytes: int = MAX_SPECIMEN_BYTES) -> OrderComparison:
    """Independently run solo and deterministic coupled/reversed/permuted orders."""
    ids = _check_test_ids(test_ids)
    _check_budget(time_limit_s, memory_limit_bytes)
    if type(seed) is not int:
        raise AuditRefusal("E_AUDIT_SCHEMA", "seed exact integer")
    solo = tuple(run_originals((name,), time_limit_s=time_limit_s,
                               memory_limit_bytes=memory_limit_bytes) for name in ids)
    scrambled = list(ids)
    random.Random(seed).shuffle(scrambled)
    orders: list[tuple[str, ...]] = []
    for order in (ids, tuple(reversed(ids)), tuple(scrambled)):
        if order not in orders:
            orders.append(order)
    coupled = tuple(run_originals(order, time_limit_s=time_limit_s,
                                  memory_limit_bytes=memory_limit_bytes) for order in orders)
    # Match every original assertion method outcome in every position; clean
    # status and no residual process state are independent mandatory predicates.
    expected = {run.requested[0]: run.cases[0][1] if len(run.cases) == 1 else "MISSING"
                for run in solo}
    agreement = (all(r.clean_pass for r in solo + coupled) and
                 all(dict(run.cases).get(i) == expected[i]
                     for run in coupled for i in run.requested))
    return OrderComparison(solo, coupled, tuple(orders), agreement)


def classify_trial(control: AuditRun, mutant: AuditRun, assertion_identity: str,
                   *, reviewed_equivalence_ref: str | None = None,
                   pinned_source_sha256: str | None = None) -> str:
    """A kill requires the designated test body's AssertionError and no leaks."""
    _nonempty(assertion_identity, "assertion_identity")
    if not control.clean_pass:
        return "BASELINE_RED" if control.status in ("ASSERTION_FAILURE", "XPASS", "XFAIL", "SKIPPED") else "INFRASTRUCTURE"
    if mutant.status == _ISOLATION or mutant.status in ("INFRASTRUCTURE", "ERROR", "XPASS", "SKIPPED", "XFAIL"):
        return "INFRASTRUCTURE"
    if (mutant.status == "ASSERTION_FAILURE" and not mutant.errors and
            len(mutant.failures) == 1 and mutant.failures[0] ==
            (assertion_identity, "AssertionError", True) and
            len(mutant.executed) == len(control.executed) and
            set(mutant.executed) == set(control.executed)):
        return "KILLED"
    if mutant.status == "ASSERTION_FAILURE":
        return "INFRASTRUCTURE"  # wrong test/phase/exception is not semantic evidence
    if mutant.clean_pass:
        if (isinstance(reviewed_equivalence_ref, str) and
                isinstance(pinned_source_sha256, str) and
                reviewed_equivalence_ref.startswith("reviewed:") and
                pinned_source_sha256 in reviewed_equivalence_ref):
            return "EQUIVALENT"
        return "SURVIVED"
    return "INFRASTRUCTURE"


def execute_mutation_trial(campaign: FrozenCampaign, site: AstSite,
                           test_ids: Sequence[str], assertion_identity: str,
                           *, source: bytes, time_limit_s: float = MAX_SPECIMEN_SECONDS,
                           memory_limit_bytes: int = MAX_SPECIMEN_BYTES,
                           reviewed_equivalence_ref: str | None = None) -> MutationTrial:
    """Pristine original first, then a single-expression mutant in new namespaces."""
    ids = _check_test_ids(test_ids)
    _check_budget(time_limit_s, memory_limit_bytes)
    if (not isinstance(campaign, FrozenCampaign) or
            site.site_id not in {s.site_id for p in campaign.populations for s in p.selected_sites} or
            site.source_sha256 != campaign.source_digests.get(site.module_path)):
        raise AuditRefusal("E_AUDIT_RECEIPT", "site not selected in frozen campaign")
    started = time.monotonic_ns()
    original = run_originals(ids, time_limit_s=time_limit_s, memory_limit_bytes=memory_limit_bytes)
    try:
        changed = mutate_source(source, site)
        mutant = run_originals(ids, time_limit_s=time_limit_s,
                               memory_limit_bytes=memory_limit_bytes,
                               _override=(site.module_path, changed))
        disposition = classify_trial(original, mutant, assertion_identity,
                                     reviewed_equivalence_ref=reviewed_equivalence_ref,
                                     pinned_source_sha256=site.source_sha256)
    except AuditRefusal as exc:
        disposition = "ANCHOR_STALE" if exc.code == "ANCHOR_STALE" else "INVALID_MUTANT"
        mutant = AuditRun(ids, (), disposition, (), (), (), (), (), (), {}, False, 0, exc.code)
    digest = hashlib.sha256(json.dumps((original.status, original.cases, original.leaks,
                                         mutant.status, mutant.cases, mutant.leaks),
                                        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return MutationTrial(MUTATION_TRIAL_SCHEMA, campaign.campaign_id, site,
                         campaign.fixture_sha256, ids, campaign.seed, original.status,
                         mutant.status, assertion_identity, digest,
                         time.monotonic_ns() - started, disposition,
                         reviewed_equivalence_ref if disposition == "EQUIVALENT" else None)
