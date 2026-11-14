# @module {"id":"wa.perceive.mark_node_join","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.transform","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Integer joins and OSC2 labels, ported from fastexec/bench/osc2.py.

HB-WA-s1-perceive: geometry evidence stays separate from rim confidence.
Hungarian candidates become bindings only after all uniqueness gates pass.
"""
from functools import cmp_to_key
from . import transform as tf
from ..state import schema

IOU_THRESHOLD_PROMILLE = 500
PROMILLE_DEN = 1_000
MARGIN_MIN_PROMILLE = 50
SWAP_SLACK_MIN_PROMILLE = 100
ROLE_COMPAT = {
    "button": frozenset(("button", "link", "menuitem", "checkbox", "radio", "switch")),
    "tab": frozenset(("tab",)),
    "field": frozenset(("textbox", "searchbox", "combobox", "spinbutton")),
    "row": frozenset(("row", "gridcell", "cell", "option", "listitem")),
    "icon": frozenset(("button", "link", "img", "menuitem")),
    "text": frozenset(("text", "StaticText", "link", "heading", "button")),
    "unknown": None,
}


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


# @anchor:wa.perceive.mark_node_join.truth
def truth(value):
    return value is True or value == "true"


# @anchor:wa.perceive.mark_node_join.actionable_nodes
def actionable_nodes(nodes):
    return [n for n in nodes if _get(n, "actionable") is True
            and truth((_get(n, "states") or {}).get("visible"))]


def _validate_unique_ids(marks, nodes):
    marks, nodes = list(marks), list(nodes)
    mids, nids = [_get(m, "id") for m in marks], [_get(n, "id") for n in nodes]
    if any(type(m) is not int or m <= 0 for m in mids) or any(not isinstance(n, str) or not n for n in nids):
        raise ValueError("invalid_join_id")
    if len(set(mids)) != len(mids) or len(set(nids)) != len(nids):
        raise ValueError("duplicate_join_ids")
    return sorted(marks, key=lambda m: _get(m, "id")), sorted(nodes, key=lambda n: _get(n, "id"))


# @anchor:wa.perceive.mark_node_join.box_overlap_int
def box_overlap_int(box_a, box_b):
    a, b = tf.xywh_to_closed(box_a), tf.xywh_to_closed(box_b)
    if a is None or b is None:
        return 0, 0, 0, False
    ov, union, _, _ = tf.closed_overlap_and_union(a, b)
    return ov, union, PROMILLE_DEN * ov // union, 2 * ov >= union


def _rim_promille(mark):
    for field in ("rim_promille", "score_promille"):
        value = _get(mark, field)
        if value is not None:
            return tf.exact_int(value, 0, PROMILLE_DEN, field)
    score = _get(mark, "score")
    return score * PROMILLE_DEN if type(score) is int and 0 <= score <= 1 else 0


def _absent(tx):
    return tx is not None and (_get(tx, "status", "certified") != "certified"
                               or type(_get(tx, "dx")) is not int or type(_get(tx, "dy")) is not int)


def _eligible(n):
    states = _get(n, "states") or {}
    return (_get(n, "actionable", True) is True and truth(states.get("visible", True))
            and truth(states.get("enabled", True)))


def _evidence(ms, ns, tx):
    edges = {}
    for i, m in enumerate(ms):
        for j, n in enumerate(ns):
            nb = _get(n, "box")
            if tx is not None:
                nb = tf.translated_box(nb, tx)
            roles = ROLE_COMPAT.get(_get(m, "kind", "unknown"), frozenset())
            compatible = roles is None or _get(n, "role") in roles
            edges[i, j] = (box_overlap_int(m, nb) if _eligible(n) and compatible
                           else (0, 0, 0, False))
    return edges


def _base_record(m, reason):
    return dict(mark_id=_get(m, "id"), node_id=None, candidate_node_id=None,
                verdict="unmatched", reason=reason, iou_num=0, iou_den=0,
                w_promille=0, margin_m=0, margin_n=0, swap_slack=0,
                rim_promille=_rim_promille(m), competitors=[], frustrated=False,
                frustration_promille=0, e2=0, mutual_best=False, in_tight_cycle=False)


def _cycle_members(pairs, weights, u, v):
    graph = {i: [k for k, l in pairs if k != i and weights[i][l] >= IOU_THRESHOLD_PROMILLE
                 and u[i + 1] + v[l + 1] == -weights[i][l]] for i, j in pairs}
    members = set()
    for start in graph:
        todo, seen = list(graph[start]), set()
        while todo:
            node = todo.pop()
            if node == start:
                members.add(start)
                break
            if node not in seen:
                seen.add(node)
                todo.extend(graph[node])
    return members


def _ambiguous_sets(records, edges, ms, ns):
    """Connected contention components, including competition for the same mark."""
    ambiguous = [r for r in records if r["frustrated"]]
    supports = {}
    for r in ambiguous:
        i = next(i for i, m in enumerate(ms) if _get(m, "id") == r["mark_id"])
        supports[r["mark_id"]] = {_get(ns[j], "id") for j in range(len(ns)) if edges[i, j][0] > 0}
    remaining = {r["mark_id"] for r in ambiguous}
    groups = []
    while remaining:
        component = {min(remaining)}
        while True:
            bound = set().union(*(supports[mid] for mid in component))
            added = {mid for mid in remaining - component if supports[mid] & bound}
            if not added:
                break
            component.update(added)
        remaining.difference_update(component)
        rows = [r for r in ambiguous if r["mark_id"] in component]
        groups.append(dict(kind="AMBIGUOUS_SET", mark_ids=sorted(component),
                           node_ids=sorted(set().union(*(supports[mid] for mid in component))),
                           pairs=[dict(mark_id=r["mark_id"], node_id=r["candidate_node_id"]) for r in rows],
                           competitors=[dict(mark_id=r["mark_id"], candidates=r["competitors"]) for r in rows],
                           frustration_promille=max(r["frustration_promille"] for r in rows),
                           e2=sum(r["e2"] for r in rows)))
    return groups


# @anchor:wa.perceive.mark_node_join.assign_marks_nodes
def assign_marks_nodes(marks, nodes, transform=None):
    ms, ns = _validate_unique_ids(marks, nodes)
    absent = _absent(transform)
    edges = {} if absent else _evidence(ms, ns, transform)
    weights = [[edges[i, j][2] if edges[i, j][3] else 0 for j in range(len(ns))]
               for i in range(len(ms))] if not absent else [[] for _ in ms]
    pairs, u, v = tf._hungarian(weights)
    assigned = dict(pairs)
    cycles = _cycle_members(pairs, weights, u, v)
    records = []
    for i, m in enumerate(ms):
        record = _base_record(m, "transform_absent" if absent else "no_overlap")
        records.append(record)
        if absent:
            continue
        candidates = sorted(((edges[i, j][2], j) for j in range(len(ns)) if edges[i, j][0]),
                            key=lambda x: (-x[0], x[1]))
        if i not in assigned:
            if candidates:
                w, j = candidates[0]
                ov, union, _, admissible = edges[i, j]
                record.update(iou_num=ov, iou_den=union, w_promille=w,
                              reason="asymmetric_best_overlap" if admissible else "iou_below_threshold")
            continue
        j = assigned[i]
        ov, union, w, _ = edges[i, j]
        rivals_m = [(edges[i, l][2], l) for l in range(len(ns)) if l != j and edges[i, l][0] > 0]
        rivals_n = [(edges[k, j][2], k) for k in range(len(ms)) if k != i and edges[k, j][0] > 0]
        margin_m = w - max((a for a, _ in rivals_m), default=0)
        margin_n = w - max((a for a, _ in rivals_n), default=0)
        mutual = (candidates[0][1] == j and
                  min(((-edges[k, j][2], k) for k in range(len(ms)) if edges[k, j][0]))[1] == i)
        slacks = [w + weights[k][l] - edges[i, l][2] - edges[k, j][2]
                  for k, l in pairs if k != i]
        slack = min(slacks + [margin_m, margin_n])
        reason = ("tight_alternating_cycle" if i in cycles else
                  "asymmetric_best_overlap" if not mutual else
                  "margin_below_50" if min(margin_m, margin_n) < MARGIN_MIN_PROMILLE else
                  "swap_slack_le_100" if slack <= SWAP_SLACK_MIN_PROMILLE else None)
        competitors = sorted((dict(id=_get(ns[l], "id"), w_promille=a) for a, l in rivals_m),
                             key=lambda c: (-c["w_promille"], c["id"]))
        record.update(node_id=_get(ns[j], "id") if reason is None else None,
                      candidate_node_id=_get(ns[j], "id"), verdict="matched" if reason is None else "ambiguous",
                      reason=reason, iou_num=ov, iou_den=union, w_promille=w,
                      margin_m=margin_m, margin_n=margin_n, swap_slack=slack,
                      competitors=competitors, mutual_best=mutual, in_tight_cycle=i in cycles,
                      frustrated=reason is not None,
                      frustration_promille=max((c["w_promille"] for c in competitors), default=0),
                      e2=sum((w - c["w_promille"])**2 for c in competitors))
    bound = {r["node_id"] for r in records if r["verdict"] == "matched"}
    out = dict(pairs=records, ambiguous_sets=_ambiguous_sets(records, edges, ms, ns),
               unmatched_marks=[r["mark_id"] for r in records if r["verdict"] != "matched"],
               unmatched_nodes=[_get(n, "id") for n in ns if _get(n, "id") not in bound],
               threshold_promille=IOU_THRESHOLD_PROMILLE)
    schema.dumps(out)
    return out


def _best(entries):
    def compare(a, b):
        delta = a[1][0] * b[1][1] - b[1][0] * a[1][1]
        return -1 if delta > 0 else 1 if delta < 0 else (a[0] > b[0]) - (a[0] < b[0])
    return sorted(entries, key=cmp_to_key(compare))[0] if entries else None


# @anchor:wa.perceive.mark_node_join.join_marks_nodes
def join_marks_nodes(marks, nodes, transform=None, *, require_mutual=False):
    ms, ns = _validate_unique_ids(marks, nodes)
    absent = _absent(transform)
    edges = {} if absent else _evidence(ms, ns, transform)
    bm = {i: _best([(_get(n, "id"), edges[i, j]) for j, n in enumerate(ns) if edges[i, j][0]])
          for i in range(len(ms))} if not absent else {}
    bn = {j: _best([(_get(m, "id"), edges[i, j]) for i, m in enumerate(ms) if edges[i, j][0]])
          for j in range(len(ns))} if not absent else {}
    by_mark, by_node = [], []
    for own, other, bests, reverse, output, own_key, other_key in (
            (ms, ns, bm, bn, by_mark, "mark_id", "node_id"),
            (ns, ms, bn, bm, by_node, "node_id", "mark_id")):
        ranks = {_get(o, "id"): k for k, o in enumerate(other)}
        for index, obj in enumerate(own):
            best = bests.get(index)
            candidate, evidence = best if best else (None, (0, 0, 0, False))
            ov, union, w, admissible = evidence
            back = reverse.get(ranks[candidate]) if candidate is not None else None
            mutual = bool(admissible and back and back[0] == _get(obj, "id"))
            accepted = admissible and (mutual or not require_mutual)
            reason = ("transform_absent" if absent else "no_overlap" if not ov else
                      "iou_below_threshold" if not admissible else
                      "asymmetric_best_overlap" if require_mutual and not mutual else None)
            output.append({own_key: _get(obj, "id"), other_key: candidate if accepted else None,
                           "matched": accepted, "mutual_matched": mutual,
                           "iou_num": ov, "iou_den": union, "iou_promille": w,
                           "verdict": "matched" if accepted else "unmatched", "reason": reason})
    assignment = assign_marks_nodes(ms, ns, transform)
    result = dict(by_mark=by_mark, by_node=by_node, pairs=assignment["pairs"],
                  ambiguous_sets=assignment["ambiguous_sets"], threshold_promille=IOU_THRESHOLD_PROMILLE)
    schema.dumps(result)
    return result


# @anchor:wa.perceive.mark_node_join.label_from_osc2
def label_from_osc2(leg, elements, nodes, joins=None, mode="marks"):
    if leg is None:
        return None, "terminal_frame"
    intent = leg["intent"]
    if intent["kind"] == "key":
        return None, "key_only"
    if leg["driver"] != "keyboard":
        return None, "lane_only"
    if intent["kind"] != "focus-key":
        return None, "unsupported_intent"
    if intent.get("key") is None:
        return None, "focus_only_not_click"
    if intent["key"] != "Return":
        return None, "unproven_activation_equivalence"
    target = intent["target"]
    elements = [e for e in elements if e.get("anchor") == target["anchor"]]
    if len(elements) != 1:
        return None, "anchor_absent" if not elements else "ambiguous_anchor"
    element = elements[0]
    if element.get("role") != target["role"] or element.get("actionable") is not True:
        return None, "target_element_not_actionable_or_role_mismatch"
    matches = [n for n in nodes if _get(n, "id") == element.get("state_node_id")]
    if len(matches) != 1:
        return None, "node_absent_or_ambiguous"
    node = matches[0]
    if node not in actionable_nodes(nodes) or not truth((_get(node, "states") or {}).get("enabled")):
        return None, "target_node_not_enabled_actionable_visible"
    if _get(node, "role") != target["role"]:
        return None, "target_node_role_mismatch"
    if mode == "nodes":
        return dict(kind="node", node_id=_get(node, "id"), anchor=target["anchor"]), None
    joins = joins or {}
    match = next((j for j in joins.get("by_node", []) if j["node_id"] == _get(node, "id")), None)
    if not match or not match["matched"]:
        return None, "target_mark_iou_below_threshold"
    forward = next((j for j in joins.get("by_mark", []) if j["mark_id"] == match["mark_id"]), None)
    if not forward or not forward["matched"] or forward["node_id"] != _get(node, "id"):
        return None, "conflicting_mark_node"
    return dict(kind="mark", mark_id=match["mark_id"], node_id=_get(node, "id"), anchor=target["anchor"],
                iou_promille=match["iou_promille"], iou_num=match["iou_num"], iou_den=match["iou_den"]), None


# @anchor:wa.perceive.mark_node_join.iou_summary
def iou_summary(promille_values):
    values = sorted(tf.exact_int(v, 0, PROMILLE_DEN) for v in promille_values)
    def percentile(q):
        if not values:
            return None
        lo, rem = divmod((len(values) - 1) * q, 100)
        hi = min(lo + 1, len(values) - 1)
        return (values[lo] * (100 - rem) + values[hi] * rem + 50) // 100
    return dict(n=len(values), minimum=values[0] if values else None,
                p50=percentile(50), p95=percentile(95), maximum=values[-1] if values else None,
                zero=sum(v == 0 for v in values), below_threshold=sum(v < IOU_THRESHOLD_PROMILLE for v in values),
                matched=sum(v >= IOU_THRESHOLD_PROMILLE for v in values))
