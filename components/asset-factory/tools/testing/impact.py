"""Conservative selection using an independently pinned exhaustive dependency inventory.

A graph is a reviewed baseline assertion about *all* inputs of a workload, including
generated inputs, external tools and skips. A trace of one execution alone cannot
establish this. An unqualified workload may always run without a graph.
"""
import os
import re
import sys
from pathlib import Path

from asset_factory import core


def graph_digest(graph):
    return core.sha256_json(graph)


def _sha(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def select(roster, inputs, toolchain, graph=None, expected_sha=None):
    """Return selected IDs in discovery order, plus the decision reason."""
    roster = list(roster)
    if not roster or len(set(roster)) != len(roster):
        raise ValueError("invalid_roster")
    if not isinstance(graph, dict) or not _sha(expected_sha) or graph_digest(graph) != expected_sha:
        return roster, "dependency_graph_unverified"
    if (graph.get("schema") != "af.test-impact/v1" or not _sha(toolchain)
            or toolchain != graph.get("toolchain_sha256")):
        return roster, "toolchain_or_schema_unknown"
    before, edges, roots, prior = (graph.get(key) for key in
                                    ("inputs", "dependencies", "roots", "prior"))
    if not all(isinstance(obj, dict) for obj in (inputs, before, edges, roots, prior)):
        return roster, "dependency_inventory_unknown"
    if (not inputs or set(inputs) != set(before) or set(edges) != set(before)
            or set(roots) != set(roster) or set(prior) != set(roster)
            or not all(_sha(v) for obj in (inputs, before) for v in obj.values())):
        return roster, "dependency_inventory_changed"

    # Walk reverse edges. Cycles are harmless: a changed strongly connected
    # component propagates once to every consumer.
    reverse = {name: set() for name in before}
    for consumer, deps in edges.items():
        if not isinstance(deps, list) or any(not isinstance(d, str) or d not in before for d in deps):
            return roster, "dependency_edge_unknown"
        for dep in deps:
            reverse[dep].add(consumer)
    for name in roster:
        deps = roots[name]
        if (not isinstance(deps, list) or not deps
                or any(not isinstance(d, str) or d not in before for d in deps)):
            return roster, "test_roots_unknown"
    # Independently recorded exhaustive per-test input sets protect against an
    # omission even when a newly built (but incomplete) graph has a valid hash.
    observed = graph.get("observed_inputs")
    if not isinstance(observed, dict) or set(observed) != set(roster):
        return roster, "exhaustive_witness_missing"
    for name in roster:
        refs = observed[name]
        if (not isinstance(refs, list) or not refs or
                any(not isinstance(dep, str) or dep not in before for dep in refs)):
            return roster, "exhaustive_witness_unknown"
    changed = {key for key in before if before[key] != inputs[key]}
    # A changed input with no known consumer is ambiguous even under an old pin.
    covered = {dep for deps in roots.values() for dep in deps}
    pending = list(covered)
    while pending:
        unseen = set(edges[pending.pop()]) - covered
        covered.update(unseen)
        pending.extend(unseen)
    if changed - covered:
        return roster, "unmapped_change"
    pending = list(changed)
    while pending:
        unseen = reverse[pending.pop()] - changed
        changed.update(unseen)
        pending.extend(unseen)
    selected = [key for key in roster if prior[key] != "ok" or changed.intersection(roots[key])
                or changed.intersection(observed[key])]
    # Fresh evidence always contains work. An empty run cannot be green.
    return (selected, "transitive_impact") if selected else (roster, "no_fresh_checks")


def inputs_from_snapshot(snapshot, root):
    return {os.path.relpath(row["path"], root): row["sha256"] for row in snapshot}


def runtime_identity(external_pin, files):
    """Reopen only caller-enrolled toolchain files and bind the running interpreter."""
    if not _sha(external_pin) or not isinstance(files, dict) or not files:
        return None
    current = {}
    for name, value in files.items():
        if not isinstance(name, str) or not isinstance(value, str):
            return None
        path = Path(value)
        if not path.is_absolute() or path != path.resolve(strict=True) or not path.is_file():
            return None
        current[name] = core.sha256_file(path)
    if core.sha256_json(current) != external_pin:
        return None
    return core.sha256_json({"external": external_pin, "python": core.sha256_file(Path(sys.executable)),
                             "version": sys.version, "platform": sys.platform})


def selection(roster, inputs, config):
    """The expected graph pin and current toolchain pin come from run configuration."""
    path = config.get("impact_graph")
    if path is None:
        return list(roster), "exhaustive"
    try:
        graph = core.load_json(Path(path), max_bytes=32 * 1024**2)
        pin = config.get("impact_sha256")
        if not _sha(pin) or graph_digest(graph) != pin:
            return list(roster), "dependency_graph_unverified"
        current = runtime_identity(config.get("toolchain_sha256"), config.get("toolchain_files"))
        return select(roster, inputs, current, graph, pin)
    except (OSError, RuntimeError, ValueError, TypeError, core.ContractError):
        return list(roster), "dependency_graph_unreadable"
