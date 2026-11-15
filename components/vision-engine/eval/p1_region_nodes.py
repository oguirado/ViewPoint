"""P1 Computer Use fusion: map vision-engine changed regions onto browser nodes.

Provides `fuse` to map regions of schema vqa.core-region/v1 onto browser DOM node records,
following Rule R10 closed-set integer predicate math.
"""

CHANGED_CLASSES = {"CHANGED", "MOVED", "FLICKER"}


MIN_PPM, MAX_PPM, DEFAULT_PPM, HALF_PPM = 100_000, 16_000_000, 1_000_000, 500_000
SCHEMA_P1_REGION_NODES = "ve.p1.region_nodes/v1"
SCHEMA_COORD_TRANSFORM = "vqa.coord-transform/v1"
W_DEN, IOU_MIN_PROMILLE, MARGIN_PROMILLE, SWAP_SLACK_PROMILLE = 1000, 500, 50, 100
TRANSFORM_MIN_SUPPORT, TRANSFORM_MAX_RESIDUAL_SQ = 3, 9


def _exact_int_coord(v):
    if type(v) is not int:
        raise TypeError("integer coordinates required; float/bool forbidden")
    return v


def _scale(v):
    v = _exact_int_coord(v)
    if not MIN_PPM <= v <= MAX_PPM:
        raise ValueError("scale_ppm out of range")
    return v


# @anchor:va.p1_region_nodes.compose_scale_ppm
def compose_scale_ppm(dpr_ppm=DEFAULT_PPM, zoom_ppm=DEFAULT_PPM):
    return _scale((_scale(dpr_ppm) * _scale(zoom_ppm) + HALF_PPM) // DEFAULT_PPM)


# @anchor:va.p1_region_nodes.project_css_coord_to_fb
def project_css_coord_to_fb(css_coord, scale_ppm=DEFAULT_PPM, d=0):
    return (_exact_int_coord(css_coord) * _scale(scale_ppm) + HALF_PPM) // DEFAULT_PPM + _exact_int_coord(d)


# @anchor:va.p1_region_nodes.css_box_to_closed_fb
def css_box_to_closed_fb(bbox_css, scale_ppm=DEFAULT_PPM, dx=0, dy=0):
    if not isinstance(bbox_css, (list, tuple)) or len(bbox_css) != 4:
        raise ValueError("four coordinates required")
    x, y, w, h = map(_exact_int_coord, bbox_css)
    _scale(scale_ppm)
    _exact_int_coord(dx)
    _exact_int_coord(dy)
    if w <= 0 or h <= 0:
        return None
    x0, y0 = project_css_coord_to_fb(x, scale_ppm, dx), project_css_coord_to_fb(y, scale_ppm, dy)
    xe, ye = project_css_coord_to_fb(x + w, scale_ppm, dx), project_css_coord_to_fb(y + h, scale_ppm, dy)
    return (x0, y0, xe - 1, ye - 1) if xe > x0 and ye > y0 else None


# @anchor:va.p1_region_nodes.closed_css_box_to_closed_fb
def closed_css_box_to_closed_fb(box, scale_ppm=DEFAULT_PPM, dx=0, dy=0):
    x0, y0, x1, y1 = map(_exact_int_coord, box)
    return css_box_to_closed_fb((x0, y0, x1-x0+1, y1-y0+1), scale_ppm, dx, dy)


def _parse_node(node, *, scale_ppm=DEFAULT_PPM, dx=0, dy=0, node_box_mode="auto"):
    if not isinstance(node, dict):
        return int(node), None, False
    i = node.get("i", node.get("node_id", node.get("id", node.get("index", 0))))
    act = bool(node.get("act", False))
    box = node.get("bbox_px")
    xywh = box is not None or node_box_mode == "xywh"
    if box is None:
        box = node.get("box")
    if box is None and all(node.get(k) is not None for k in ("x0", "y0", "x1", "y1")):
        box = [node[k] for k in ("x0", "y0", "x1", "y1")]
    if box is None:
        return i, None, act
    convert = css_box_to_closed_fb if xywh else closed_css_box_to_closed_fb
    return i, convert(box, scale_ppm, dx, dy), act


def _region_to_closed_box(bbox_px):
    return css_box_to_closed_fb(bbox_px)


_region_to_closed_fb = _region_to_closed_box


def _boxes_overlap(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
    """Rule R10: closed-set predicate ax0 <= bx1 and bx0 <= ax1 and ay0 <= by1 and by0 <= ay1."""
    return (ax0 <= bx1) and (bx0 <= ax1) and (ay0 <= by1) and (by0 <= ay1)


def _overlap_area(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
    """Compute overlap area using Python integers."""
    if not _boxes_overlap(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
        return 0
    ox0 = max(ax0, bx0)
    ox1 = min(ax1, bx1)
    oy0 = max(ay0, by0)
    oy1 = min(ay1, by1)
    return (ox1 - ox0 + 1) * (oy1 - oy0 + 1)


# @anchor:va.p1_region_nodes.fuse
def fuse(regions, nodes, k=5, dom_changed_nodes=None, *, scale_ppm=DEFAULT_PPM, dx=0, dy=0, transform_status="certified", include_envelope=False):
    """Fuse vision-engine regions and browser nodes.

    Parameters
    ----------
    regions : list
        List of region records (schema vqa.core-region/v1).
    nodes : list
        List of browser node records (dict with box [x0, y0, x1, y1] or x0..y1, act, i).
    k : int, optional
        Maximum top overlapping nodes to return per region (default 5).
    dom_changed_nodes : list, optional
        List of node records or node indices 'i' changed in the DOM.

    Returns
    -------
    dict
        Deterministic dict serializable with json.dumps sort_keys True.
    """
    if include_envelope:
        return fuse_envelope(regions, nodes, k, dom_changed_nodes, scale_ppm=scale_ppm, dx=dx, dy=dy, transform_status=transform_status)
    certified = transform_status == "certified" and dx is not None and dy is not None
    if certified:
        project_css_coord_to_fb(0, scale_ppm, dx)
        project_css_coord_to_fb(0, scale_ppm, dy)
    parsed_nodes = [_parse_node(n, scale_ppm=scale_ppm, dx=dx, dy=dy) if certified else (_parse_node(n)[0], None, bool(n.get("act", False)) if isinstance(n, dict) else False) for n in nodes]

    changed_regions = [r for r in regions if r.get("cls") in CHANGED_CLASSES]

    mapped_regions = []
    visual_only = []

    for r in changed_regions:
        r_id = r.get("id", "")
        r_cls = r.get("cls", "")
        bbox_px = [_exact_int_coord(v) for v in r.get("bbox_px", [0, 0, 0, 0])]
        reg_box = _region_to_closed_box(bbox_px)

        overlapping = []
        actionable_overlapping = []

        if reg_box is not None:
            ax0, ay0, ax1, ay1 = reg_box
            for node_rec, (i, n_box, act) in zip(nodes, parsed_nodes):
                if n_box is None:
                    continue
                bx0, by0, bx1, by1 = n_box
                ov_area = _overlap_area(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1)
                if ov_area > 0:
                    n_area = (bx1 - bx0 + 1) * (by1 - by0 + 1)
                    item = {
                        "i": i,
                        "overlap_area": ov_area,
                        "node_box_area": n_area,
                        "act": act,
                    }
                    overlapping.append(item)
                    if act:
                        actionable_overlapping.append(item)

        if not overlapping:
            rec = {
                "id": r_id,
                "region_id": r_id,
                "cls": r_cls,
                "bbox_px": bbox_px,
                "nodes": [],
                "smallest_actionable_node_i": None,
            }
            visual_only.append(rec)
        else:
            # Sort overlapping nodes by overlap_area DESC, node_box_area ASC, i ASC
            overlapping.sort(key=lambda x: (-x["overlap_area"], x["node_box_area"], x["i"]))

            # Find smallest actionable node among overlapping actionable nodes
            smallest_act_i = None
            if actionable_overlapping:
                actionable_overlapping.sort(key=lambda x: (x["node_box_area"], x["i"]))
                smallest_act_i = actionable_overlapping[0]["i"]

            top_k = []
            for item in overlapping[:k]:
                node_entry = {
                    "i": item["i"],
                    "overlap_area": item["overlap_area"],
                    "node_box_area": item["node_box_area"],
                    "act": item["act"],
                    "smallest_actionable": (item["i"] == smallest_act_i),
                }
                top_k.append(node_entry)

            rec = {
                "id": r_id,
                "region_id": r_id,
                "cls": r_cls,
                "bbox_px": bbox_px,
                "nodes": top_k,
                "smallest_actionable_node_i": smallest_act_i,
            }
            mapped_regions.append(rec)

    # DOM-only check
    dom_only_set = set()
    if dom_changed_nodes is not None:
        # Precompute closed boxes for changed regions
        chg_boxes = []
        for r in changed_regions:
            b = _region_to_closed_box(r.get("bbox_px", [0, 0, 0, 0]))
            if b is not None:
                chg_boxes.append(b)

        node_map = {i: (n_box, act) for i, n_box, act in parsed_nodes}

        for item in dom_changed_nodes:
            dom_i = int(item.get("i", item.get("index"))) if isinstance(item, dict) else int(item)
            n_box, _ = node_map.get(dom_i, (None, False))
            if n_box is None:
                dom_only_set.add(dom_i)
            else:
                bx0, by0, bx1, by1 = n_box
                has_overlap = False
                for ax0, ay0, ax1, ay1 in chg_boxes:
                    if _boxes_overlap(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
                        has_overlap = True
                        break
                if not has_overlap:
                    dom_only_set.add(dom_i)

    dom_only = sorted(dom_only_set)

    return {
        "dom_only": dom_only,
        "regions": mapped_regions,
        "visual_only": visual_only,
    }


def _hungarian(weights):
    """Rectangular exact minimum-cost potentials for negated weights."""
    nr = len(weights)
    nc = len(weights[0]) if nr else 0
    # Identical rows have an exact closed-form optimum and feasible integer duals.
    if nr and all(row == weights[0] for row in weights[1:]):
        chosen = sorted(range(nc), key=lambda j: (-weights[0][j], j))[:nr]
        threshold = weights[0][chosen[-1]]
        return sorted(chosen), [-threshold]*nr, [min(0, threshold-w) for w in weights[0]]
    u, v, p, way = [0]*(nr+1), [0]*(nc+1), [0]*(nc+1), [0]*(nc+1)
    inf = 1 + sum(abs(w) for row in weights for w in row)
    for i in range(1, nr+1):
        p[0], j0 = i, 0
        minv, used = [inf]*(nc+1), [False]*(nc+1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, nc+1):
                if not used[j]:
                    cur = -weights[i0-1][j-1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(nc+1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    assigned = [-1]*nr
    for j in range(1, nc+1):
        if p[j]:
            assigned[p[j]-1] = j-1
    return assigned, u[1:], v[1:]


def _canonical_assignment(weights, assignment, u, v):
    """Choose the lexicographically first optimum using tight alternating paths.

    Fixed earlier rows cannot move. Zero-cost dummy rows preserve the dual
    constraints on unused columns in the rectangular problem.
    """
    nr = len(assignment)
    if not nr:
        return []
    if all(row == weights[0] for row in weights[1:]):
        return sorted(sorted(range(len(v)), key=lambda j: (-weights[0][j], j))[:nr])
    assignment = list(assignment)
    free = sorted(set(range(len(v))) - set(assignment))
    assignment.extend(free)
    owners = {j: i for i, j in enumerate(assignment)}
    tight = [[j for j, w in enumerate(row) if -w == u[i]+v[j]]
             for i, row in enumerate(weights)]
    # Zero-cost dummy rows account for the rectangular solver's unused columns.
    tight.extend([[j for j in range(len(v)) if v[j] == 0]]*len(free))
    for fixed in range(nr):
        old = assignment[fixed]
        del owners[old]
        seen = set()

        def augment(row):
            for col in tight[row]:
                if col in seen:
                    continue
                seen.add(col)
                owner = owners.get(col)
                if owner is not None and (owner <= fixed or not augment(owner)):
                    continue
                owners[col] = row
                assignment[row] = col
                return True
            return False

        if not augment(fixed):
            raise AssertionError("optimal matching lost its tight augmenting path")
    return assignment[:nr]


def _area(box):
    return (box[2]-box[0]+1)*(box[3]-box[1]+1) if box else 0


def _id_key(value):
    if type(value) is int:
        return (0, value)
    if isinstance(value, str):
        return (1, value)
    raise TypeError("join IDs must be integer or string")


def _mark_id(mark, index):
    return mark.get("mark_id", mark.get("id", mark.get("region_id", index)))


def _evidence(pairs, nr, nn, overlaps, unmatched_dom=0, n_dom=0):
    ambiguous = sum(p["verdict"] == "ambiguous" for p in pairs)
    orphan = sum(p["verdict"] == "unmatched" for p in pairs)
    penalty = ambiguous + orphan + unmatched_dom
    competing = sum(p["e2"] for p in pairs)
    margins = [min(p["margin_m"], p["margin_n"]) for p in pairs if p["candidate_node_id"] is not None]
    slacks = [p["swap_slack"] for p in pairs if p["candidate_node_id"] is not None]
    frustration = max([1000*penalty // max(1, nr+n_dom)] + [p["frustration_promille"] for p in pairs])
    e2 = dict(energy=competing+1_000_000*penalty, competing_overlap_energy=competing,
              pairs_evaluated=nr*nn, pairs_overlapping=overlaps,
              competing_regions=sum(p["overlap_count"] >= 2 for p in pairs),
              min_margin_promille=min(margins) if margins else None,
              min_swap_slack_promille=min(slacks) if slacks else None,
              n_ambiguous=ambiguous, n_orphan_regions=orphan, n_unmatched_dom_changed=unmatched_dom)
    return frustration, e2


# @anchor:va.p1_region_nodes.assign_marks_nodes
def assign_marks_nodes(marks, nodes, *, scale_ppm=DEFAULT_PPM, dx=0, dy=0,
                       transform_status="certified", mark_box_mode="xywh", node_box_mode="auto"):
    """Exact bijective assignment; uncertainty never commits a candidate binding."""
    if mark_box_mode not in ("xywh", "closed") or node_box_mode not in ("auto", "closed", "xywh"):
        raise ValueError("invalid box mode")
    ms = sorted([(_mark_id(m, i), m) for i, m in enumerate(marks)], key=lambda x: _id_key(x[0]))
    ns = sorted([(_parse_node(n)[0], n) for n in nodes], key=lambda x: _id_key(x[0]))
    if len({m[0] for m in ms}) != len(ms) or len({n[0] for n in ns}) != len(ns):
        raise ValueError("duplicate_join_ids")
    certified = transform_status == "certified" and dx is not None and dy is not None
    if certified:
        project_css_coord_to_fb(0, scale_ppm, dx)
        project_css_coord_to_fb(0, scale_ppm, dy)
    mb = []
    for _, m in ms:
        b = m.get("bbox_px", m.get("box", [0, 0, 0, 0]))
        mb.append(_region_to_closed_box(b) if mark_box_mode == "xywh" else closed_css_box_to_closed_fb(b))
    nb = [_parse_node(n, scale_ppm=scale_ppm, dx=dx, dy=dy, node_box_mode=node_box_mode)[1] if certified else None for _, n in ns]
    nr, nn = len(ms), len(ns)
    overlap, union, weights = [], [], []
    node_areas = [_area(b) for b in nb]
    for a in mb:
        mark_area = _area(a)
        ovs = [_overlap_area(*a, *b) if a and b else 0 for b in nb]
        uns = [mark_area+area-o for area, o in zip(node_areas, ovs)]
        overlap.append(ovs)
        union.append(uns)
        weights.append([1000*o//u if o else 0 for o, u in zip(ovs, uns)])
    matrix = [[w if 2*o >= un and o else 0 for w, o, un in zip(ws, ovs, uns)] + [0]*nr
              for ws, ovs, uns in zip(weights, overlap, union)]
    assignment, u, v = _hungarian(matrix)
    assignment = _canonical_assignment(matrix, assignment, u, v)
    assigned = {i: j for i, j in enumerate(assignment) if j < nn and matrix[i][j] >= 500}
    reach = {i: {h for h, j in assigned.items() if h != i and matrix[i][j] >= 500 and -matrix[i][j] == u[i]+v[j]} for i in assigned}
    for k in assigned:
        for i in assigned:
            if k in reach[i]:
                reach[i] |= reach[k]
    pairs = []
    for i, (mid, _) in enumerate(ms):
        j = assigned.get(i)
        candidates = sorted([dict(i=ns[h][0], w_promille=max(1, w), overlap_area=overlap[i][h])
                             for h, w in enumerate(weights[i]) if overlap[i][h]], key=lambda c: (-c["w_promille"], _id_key(c["i"])))
        ws = [c["w_promille"] for c in candidates]
        energy = sum(w*w+(ws[0]-w)**2 for w in ws[1:])
        best = max(weights[i], default=0)
        w = weights[i][j] if j is not None else best
        margin_m = w-max([0]+[val for h, val in enumerate(weights[i]) if h != j]) if j is not None else 0
        margin_n = w-max([0]+[weights[h][j] for h in range(nr) if h != i]) if j is not None else 0
        slacks = [w+weights[h][k]-weights[i][k]-weights[h][j] for h, k in assigned.items()
                  if h != i and (weights[i][k] or weights[h][j])] if j is not None else []
        slack = min(slacks) if slacks else w
        reason = "unique_hungarian_match"
        verdict = "matched"
        if j is None:
            verdict = "unmatched"
            reason = "transform_absent" if not certified else "below_iou_gate" if candidates else "zero_overlap"
        elif i in reach[i]:
            verdict, reason = "ambiguous", "tight_alternating_cycle"
        elif slack <= 100:
            verdict, reason = "ambiguous", "swap_slack_le_100"
        elif margin_m < 50:
            verdict, reason = "ambiguous", "margin_m_lt_50"
        elif margin_n < 50:
            verdict, reason = "ambiguous", "margin_n_lt_50"
        frustration = ws[1] if len(ws) > 1 else 0
        if verdict == "ambiguous":
            frustration = max(frustration, 1000-min(margin_m, margin_n, slack))
        if verdict == "unmatched":
            frustration = 1000
        evidence_j = j if j is not None else (max(range(nn), key=lambda h: weights[i][h]) if candidates else None)
        pairs.append(dict(mark_id=mid, node_id=ns[j][0] if verdict == "matched" else None,
                          candidate_node_id=ns[j][0] if j is not None else None,
                          verdict=verdict, reason=reason, frustrated=verdict != "matched",
                          iou_num=overlap[i][evidence_j] if evidence_j is not None else 0,
                          iou_den=union[i][evidence_j] if evidence_j is not None else 1,
                          w_promille=w, margin_m=margin_m, margin_n=margin_n, swap_slack=slack,
                          in_tight_cycle=i in reach.get(i, set()),
                          competitors=[c for c in candidates if j is None or c["i"] != ns[j][0]],
                          overlap_count=len(candidates), frustration_promille=frustration, e2=energy))
    groups, remaining = [], {i for i, p in enumerate(pairs) if p["verdict"] == "ambiguous"}
    while remaining:
        component = {min(remaining)}
        changed = True
        while changed:
            ids = {j for i in component for j, o in enumerate(overlap[i]) if o}
            added = {i for i in remaining if any(overlap[i][j] for j in ids)}
            changed = not added <= component
            component |= added
        remaining -= component
        group_pairs = [pairs[i] for i in sorted(component)]
        groups.append(dict(kind="AMBIGUOUS_SET", mark_ids=[p["mark_id"] for p in group_pairs],
                           node_ids=[ns[j][0] for j in range(nn) if any(overlap[i][j] for i in component)],
                           competitors=[dict(mark_id=p["mark_id"], nodes=p["competitors"]) for p in group_pairs],
                           reason=group_pairs[0]["reason"], frustration_promille=max(p["frustration_promille"] for p in group_pairs),
                           e2=sum(p["e2"] for p in group_pairs)))
    matched = [p for p in pairs if p["verdict"] == "matched"]
    used = {p["node_id"] for p in matched}
    unmatched_nodes = []
    for j, (nid, n) in enumerate(ns):
        if nid not in used:
            reason = "boxless" if nb[j] is None else "no_region_overlap" if not any(row[j] for row in overlap) else "below_iou_gate"
            unmatched_nodes.append(dict(i=nid, act=bool(n.get("act", False)) if isinstance(n, dict) else False, reason=reason))
    frustration, e2 = _evidence(pairs, nr, nn, sum(o > 0 for row in overlap for o in row))
    return dict(schema=SCHEMA_P1_REGION_NODES, pairs=pairs, matched_pairs=matched,
                ambiguous_sets=groups, unmatched_marks=[p for p in pairs if p["verdict"] == "unmatched"],
                unmatched_nodes=unmatched_nodes, frustration_promille=frustration, e2=e2,
                passed_gate=bool(nr) and frustration == 0 and e2["energy"] == 0)


# @anchor:va.p1_region_nodes.fuse_envelope
def fuse_envelope(regions, nodes, k=5, dom_changed_nodes=None, *, scale_ppm=DEFAULT_PPM,
                  dx=0, dy=0, transform_status="certified"):
    """Add integer evidence while preserving the legacy ranked fusion records."""
    options = dict(scale_ppm=scale_ppm, dx=dx, dy=dy, transform_status=transform_status)
    result = fuse(regions, nodes, k, dom_changed_nodes, **options)
    changed = [r for r in regions if r.get("cls") in CHANGED_CLASSES]
    join = assign_marks_nodes(changed, nodes, **options)
    pair_map = {p["mark_id"]: p for p in join["pairs"]}
    bindings, orphans = [], []
    mapped = {r["region_id"]: r for r in result["regions"]}
    for index, r in enumerate(changed):
        rid = _mark_id(r, index)
        p = pair_map[rid]
        rec = mapped.get(r.get("id", ""))
        if p["verdict"] == "unmatched":
            orphans.append(dict(region_id=rid, cls=r["cls"], bbox_px=list(r["bbox_px"]),
                                reason=p["reason"]))
            continue
        entries = []
        for n in rec["nodes"]:
            union = _area(_region_to_closed_box(r["bbox_px"])) + n["node_box_area"] - n["overlap_area"]
            entries.append(dict(n, iou_promille=1000*n["overlap_area"]//union, margin_promille=p["margin_m"]))
        bindings.append(dict(region_id=rid, cls=r["cls"], bbox_px=list(r["bbox_px"]),
                             verdict="bound" if p["verdict"] == "matched" else "ambiguous",
                             assigned_node_i=p["node_id"], smallest_actionable_node_i=rec["smallest_actionable_node_i"],
                             nodes=entries, **{key: p[key] for key in ("iou_num", "iou_den", "w_promille", "margin_m", "margin_n", "swap_slack", "frustration_promille", "e2", "competitors")}))
    dom_ids = {item.get("i", item.get("index")) if isinstance(item, dict) else item for item in (dom_changed_nodes or [])}
    committed = {p["node_id"] for p in join["matched_pairs"]}
    missing_dom = len(dom_ids-committed)
    frustration, energy = _evidence(join["pairs"], len(changed), len(nodes), join["e2"]["pairs_overlapping"], missing_dom, len(dom_ids))
    failures = energy["n_orphan_regions"] + energy["n_ambiguous"] + missing_dom
    frustration = max([frustration]+[p["frustration_promille"] for p in join["pairs"]]+[1000*failures//max(1, len(changed)+len(dom_ids))])
    result.update(schema=SCHEMA_P1_REGION_NODES, region_bindings=bindings, orphan_regions=orphans,
                  unmatched_nodes=join["unmatched_nodes"], ambiguous_sets=join["ambiguous_sets"],
                  frustration_promille=frustration, e2=energy,
                  passed_gate=bool(changed or dom_ids) and failures == 0 and frustration == 0 and energy["energy"] == 0)
    return result
