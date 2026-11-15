# @module {"id":"wa.tools.state_budget","layer":0,"owner":"ARCH","deps":["wa.state.schema"],"book":"HB-WA-a0-arch"}
"""@module wa.tools.state_budget - STATE size and token budget reporter and trimmer.

HB-WA-a0-arch Q3 per-STATE token budget reporter and budget enforcer tool.
"""
import copy
import json
import math
import sys
from wender.state import schema


# @anchor:wa.tools.state_budget.report
def report(state):
    """Compute size metrics and estimated token budget for a STATE object.

    Returns a dictionary with integer values only.
    Ratios are reported in ppm (parts per million).
    Token count uses heuristic ceil(bytes / 4) labelled tokens_estimate.
    """
    errs = schema.validate_state(state)
    if errs:
        raise schema.StateError("; ".join(errs))

    dump_str = schema.dumps(state)
    bytes_total = len(dump_str.encode("utf-8"))

    nodes = state.get("nodes", [])
    nodes_str = schema.dumps(nodes)
    bytes_nodes = len(nodes_str.encode("utf-8"))
    bytes_top_level = bytes_total - bytes_nodes

    node_count = len(nodes)
    bytes_per_node = bytes_total // node_count if node_count > 0 else 0

    box_bytes = 0
    states_bytes = 0
    name_bytes = 0

    for n in nodes:
        box_bytes += len(schema.dumps(n["box"]).encode("utf-8"))
        states_bytes += len(schema.dumps(n["states"]).encode("utf-8"))
        name_bytes += len(schema.dumps(n["name"]).encode("utf-8"))

    if bytes_total > 0:
        box_ppm = (box_bytes * 1_000_000) // bytes_total
        states_ppm = (states_bytes * 1_000_000) // bytes_total
        name_ppm = (name_bytes * 1_000_000) // bytes_total
    else:
        box_ppm = 0
        states_ppm = 0
        name_ppm = 0

    tokens_estimate = math.ceil(bytes_total / 4)

    return {
        "bytes_total": bytes_total,
        "bytes_nodes": bytes_nodes,
        "bytes_top_level": bytes_top_level,
        "bytes_per_node": bytes_per_node,
        "box_bytes": box_bytes,
        "states_bytes": states_bytes,
        "name_bytes": name_bytes,
        "box_ppm": box_ppm,
        "states_ppm": states_ppm,
        "name_ppm": name_ppm,
        "tokens_estimate": tokens_estimate,
    }


# @anchor:wa.tools.state_budget.trim
def trim(state, max_bytes):
    """Trim non-actionable leaf nodes deepest first until canonical dump <= max_bytes.

    Keeps all actionable nodes and their emitted ancestors.
    Recomputes actionable and counts. Keeps hashes unchanged.
    Raises ValueError if budget is impossible.
    """
    errs = schema.validate_state(state)
    if errs:
        raise schema.StateError("; ".join(errs))

    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")

    current_state = copy.deepcopy(state)
    nodes = current_state["nodes"]

    # Identify protected node ids (actionable nodes + all their emitted ancestors)
    node_by_id = {n["id"]: n for n in nodes}
    actionable_ids = set(current_state.get("actionable", []))
    protected_ids = set()

    for aid in actionable_ids:
        curr = aid
        while curr is not None and curr in node_by_id:
            protected_ids.add(curr)
            curr = node_by_id[curr]["parent"]

    # Iteratively trim candidate leaf nodes deepest first
    while True:
        dump_bytes = len(schema.dumps(current_state).encode("utf-8"))
        if dump_bytes <= max_bytes:
            break

        # Find leaves (nodes that are not parents of any remaining node)
        parent_ids = {n["parent"] for n in current_state["nodes"] if n["parent"] is not None}
        candidate_indices = [
            i for i, n in enumerate(current_state["nodes"])
            if n["id"] not in protected_ids and n["id"] not in parent_ids
        ]

        if not candidate_indices:
            raise ValueError(
                "impossible budget: max_bytes=%d too small for protected nodes size %d"
                % (max_bytes, dump_bytes)
            )

        # Pick deepest leaf node; tie-break deterministically by highest index in document order
        target_index = max(
            candidate_indices,
            key=lambda i: (current_state["nodes"][i]["depth"], i)
        )

        del current_state["nodes"][target_index]

        # Recompute actionable list and counts
        remaining_nodes = current_state["nodes"]
        actionable_list = [n["id"] for n in remaining_nodes if n["actionable"]]
        current_state["actionable"] = actionable_list
        current_state["counts"] = {
            "raw": current_state["counts"]["raw"],
            "nodes": len(remaining_nodes),
            "actionable": len(actionable_list),
        }

    val_errs = schema.validate_state(current_state)
    if val_errs:
        raise schema.StateError("; ".join(val_errs))

    return current_state


# @anchor:wa.tools.state_budget.main
def main(argv=None):
    """CLI entry point: reads STATE JSON file path and prints report via schema.dumps."""
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        print("Usage: python3 -m tools.state_budget <path-to-state.json>", file=sys.stderr)
        return 1
    filepath = argv[0]
    with open(filepath, "r", encoding="utf-8") as f:
        st = json.load(f)
    rep = report(st)
    print(schema.dumps(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
