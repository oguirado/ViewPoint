# @module {"id":"wa.state.diff","layer":0,"owner":"STATE","deps":["wa.state.schema"],"book":"HB-WA-a0-arch"}
"""Deterministic semantic changes between validated STATE snapshots.

Matched nodes follow the after document order. Box-only changes are ordered ids
in layout_only; mixed semantic/layout changes retain box in the changed record.
The diff digest covers the complete diff payload, including layout information;
it does not change either input STATE's accessibility hashes.
"""
from copy import deepcopy
import hashlib

from wender.state.schema import StateError, dumps, validate_state

_FIELDS = ("name", "states", "actionable", "parent", "depth", "box")


# @anchor:wa.state.diff.diff_states
def diff_states(before, after):
    """Return a canonical-serializable diff without mutating either STATE.

    states_flipped maps changed ids to sorted state attribute names. Only ids
    with a state attribute change occur in that mapping. Actionability gains
    and losses include newly added and removed actionable nodes respectively.
    """
    for label, state in (("before", before), ("after", after)):
        errors = validate_state(state)
        if errors:
            raise StateError(label + ": " + "; ".join(errors))

    old = {node["id"]: node for node in before["nodes"]}
    new = {node["id"]: node for node in after["nodes"]}
    result = {
        "schema": "wender.state_diff/v1",
        "added": [node["id"] for node in after["nodes"] if node["id"] not in old],
        "removed": [node["id"] for node in before["nodes"] if node["id"] not in new],
        "changed": [],
        "layout_only": [],
        "states_flipped": {},
        "actionable_gained": [node["id"] for node in after["nodes"]
                              if node["actionable"] and
                              (node["id"] not in old or not old[node["id"]]["actionable"])],
        "actionable_lost": [node["id"] for node in before["nodes"]
                            if node["actionable"] and
                            (node["id"] not in new or not new[node["id"]]["actionable"])],
        "same_chassis": before["ax_stable_sha256"] == after["ax_stable_sha256"],
    }
    for node in after["nodes"]:
        identity = node["id"]
        if identity not in old:
            continue
        previous = old[identity]
        fields = sorted(key for key in _FIELDS if previous[key] != node[key])
        if fields == ["box"]:
            result["layout_only"].append(identity)
            continue
        if not fields:
            continue
        result["changed"].append({
            "id": identity, "fields": fields,
            "before": {key: deepcopy(previous[key]) for key in fields},
            "after": {key: deepcopy(node[key]) for key in fields},
        })
        if "states" in fields:
            old_states, new_states = previous["states"], node["states"]
            result["states_flipped"][identity] = sorted(
                key for key in old_states.keys() | new_states.keys()
                if key not in old_states or key not in new_states or old_states[key] != new_states[key])

    result["counts"] = {key: len(result[key]) for key in ("added", "removed", "changed")}
    result["diff_sha256"] = hashlib.sha256(dumps(result).encode("utf-8")).hexdigest()
    return result


# @anchor:wa.state.diff.summarize
def summarize(diff):
    """Return a one-line ASCII count summary bounded to 200 bytes."""
    counts = diff["counts"]
    return ("plus %d minus %d changed %d" %
            (counts["added"], counts["removed"], counts["changed"]))[:200]
