"""Deterministic, integer-only per-actor attribution of changed screen regions.

Track P reference implementation (standalone, standard library only).
``swarm/`` must never import this module.

Given the ``changed_regions`` (``[x, y, w, h]``) observed between
``dispatch_capture_seq`` and ``verify_frame_ref``, the active actor's declared
``footprint`` and the regions that concurrent or prior peers declared they
expect to change, every changed pixel is put in exactly one class:

    own       inside the footprint, expected by no peer
    spill     outside the footprint, expected by no peer
    foreign   expected by a peer (contested when also inside the footprint)

Verdict precedence (first match wins)::

    FOOTPRINT_MISSING       footprint absent or not a valid [x, y, w, h]
    STALE_FRAME             frame order is inconsistent, or a change between
                            based_on_frame and dispatch_capture_seq touches the
                            target/footprint (the planning baseline is invalid)
    FOREIGN_CHANGE_REFUSED  a peer-expected change lies inside the footprint,
                            or an unexpected change touches a target that lies
                            outside the footprint
    NO_VISIBLE_CHANGE       nothing changed at all
    VERIFIED_WITH_SPILL     unexpected change outside the footprint, or no
                            change inside the footprint at all
    VERIFIED                every changed pixel is inside the footprint and
                            expected by no peer; peer-expected changes away
                            from the footprint are reported, never hidden

Negative-control law: ``VERIFIED`` implies ``spill_regions == []``,
``contested_regions == []`` and a valid baseline.  ``attribute`` asserts it.

All arithmetic is on Python ``int``; floats and bools are rejected.  Output
rectangles are disjoint, canonical (independent of input order and of how the
input was split into rectangles) and sorted by ``(y, x, w, h)``.
"""

VERIFIED = "VERIFIED"
VERIFIED_WITH_SPILL = "VERIFIED_WITH_SPILL"
NO_VISIBLE_CHANGE = "NO_VISIBLE_CHANGE"
STALE_FRAME = "STALE_FRAME"
FOREIGN_CHANGE_REFUSED = "FOREIGN_CHANGE_REFUSED"
FOOTPRINT_MISSING = "FOOTPRINT_MISSING"

REFUSALS = (STALE_FRAME, FOREIGN_CHANGE_REFUSED, FOOTPRINT_MISSING)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_rect(value):
    """True for ``[x, y, w, h]`` of non-negative ints with ``w, h > 0``."""
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(_is_int(v) for v in value)
            and value[0] >= 0 and value[1] >= 0 and value[2] > 0 and value[3] > 0)


def _rects(values, what):
    out = []
    for value in values or ():
        if not is_rect(value):
            raise ValueError("%s: %r is not an integer [x, y, w, h] rect" % (what, value))
        out.append(tuple(value))
    return out


def intersects(a, b):
    return (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
            and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])


def _covers(rects, x, y):
    for r in rects:
        if r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]:
            return True
    return False


def _merge_cells(cells, xs, ys):
    """Turn a set of grid cells ``(row, col)`` into canonical disjoint rects.

    Maximal horizontal runs per grid row, then vertical merge of runs with the
    same x-extent in adjacent rows.  The result depends only on the covered
    pixel set, because rows with equal run structure merge whatever grid lines
    happened to separate them.
    """
    open_runs = {}  # (x0, x1) -> [y0, y1] currently extendable
    done = []
    for row in range(len(ys) - 1):
        runs, start = [], None
        for col in range(len(xs)):
            inside = col < len(xs) - 1 and (row, col) in cells
            if inside and start is None:
                start = col
            elif not inside and start is not None:
                runs.append((xs[start], xs[col]))
                start = None
        nxt = {}
        for run in runs:
            span = open_runs.pop(run, None)
            if span is not None and span[1] == ys[row]:
                span[1] = ys[row + 1]
            else:
                if span is not None:
                    done.append((run, span))
                span = [ys[row], ys[row + 1]]
            nxt[run] = span
        done.extend(open_runs.items())
        open_runs = nxt
    done.extend(open_runs.items())
    rects = [[x0, y0, x1 - x0, y1 - y0] for (x0, x1), (y0, y1) in done]
    rects.sort(key=lambda r: (r[1], r[0], r[2], r[3]))
    return rects


def normalize(regions):
    """Canonical disjoint cover of the union of ``regions``."""
    rects = _rects(regions, "regions")
    xs = sorted({r[0] for r in rects} | {r[0] + r[2] for r in rects})
    ys = sorted({r[1] for r in rects} | {r[1] + r[3] for r in rects})
    cells = {(j, i) for j in range(len(ys) - 1) for i in range(len(xs) - 1)
             if _covers(rects, xs[i], ys[j])}
    return _merge_cells(cells, xs, ys)


def area(regions):
    return sum(r[2] * r[3] for r in normalize(regions))


def diff_regions(before, after, width, height, channels, tile=16):
    """Changed regions between two raw 8-bit pixel buffers, on a tile grid.

    A tile is changed when any byte in it differs.  Returns canonical rects
    aligned to ``tile`` (clipped to the image).  Integer-only, deterministic.
    """
    for value in (width, height, channels, tile):
        if not _is_int(value) or value < 1:
            raise ValueError("width, height, channels and tile must be ints >= 1")
    stride = width * channels
    if len(before) != stride * height or len(after) != stride * height:
        raise ValueError("pixel buffers do not match width*height*channels")
    a, b = memoryview(bytes(before)), memoryview(bytes(after))
    cols = (width + tile - 1) // tile
    rows = (height + tile - 1) // tile
    cells = set()
    for y in range(height):
        base = y * stride
        if a[base:base + stride] == b[base:base + stride]:
            continue
        row = y // tile
        for col in range(cols):
            if (row, col) in cells:
                continue
            lo = base + col * tile * channels
            hi = min(base + stride, lo + tile * channels)
            if a[lo:hi] != b[lo:hi]:
                cells.add((row, col))
    xs = [min(width, c * tile) for c in range(cols + 1)]
    ys = [min(height, r * tile) for r in range(rows + 1)]
    return _merge_cells(cells, xs, ys)


def _peer_list(peers):
    out = []
    for peer in peers or ():
        actor_id = peer.get("actor_id", peer.get("agent_id"))
        if not isinstance(actor_id, str) or not actor_id:
            raise ValueError("peer expectation needs a non-empty actor_id")
        regions = list(peer.get("regions") or ())
        if peer.get("footprint") is not None:
            regions.append(peer["footprint"])
        out.append((actor_id, _rects(regions, "peer %s" % actor_id)))
    out.sort(key=lambda item: item[0])
    return out


def _result(verdict, reason, dispatched, based_on_frame, dispatch_capture_seq,
            verify_frame_ref, **regions):
    out = {
        "verdict": verdict,
        "refused": verdict in REFUSALS,
        "dispatched": dispatched,
        "reason": reason,
        "based_on_frame": based_on_frame,
        "dispatch_capture_seq": dispatch_capture_seq,
        "verify_frame_ref": verify_frame_ref,
        "rebased": (dispatched and _is_int(based_on_frame)
                    and _is_int(dispatch_capture_seq)
                    and based_on_frame < dispatch_capture_seq),
        "own_regions": [],
        "spill_regions": [],
        "foreign_peer_regions": [],
        "contested_regions": [],
        "foreign_by_actor": {},
        "baseline_conflict_regions": [],
    }
    out.update(regions)
    return out


def check_baseline(footprint, baseline_regions, target_region=None):
    """Regions changed since ``based_on_frame`` that invalidate the plan.

    ``baseline_regions`` are the changes between ``based_on_frame`` and
    ``dispatch_capture_seq`` (the union of the sidecars' ``changed_regions``).
    Returns the canonical part of them that lies inside the footprint or the
    target; an empty list means the act may be validly rebased.
    """
    guard = [tuple(footprint)] + ([tuple(target_region)] if target_region else [])
    base = _rects(baseline_regions, "baseline_regions")
    xs = sorted({v for r in base + guard for v in (r[0], r[0] + r[2])})
    ys = sorted({v for r in base + guard for v in (r[1], r[1] + r[3])})
    cells = {(j, i) for j in range(len(ys) - 1) for i in range(len(xs) - 1)
             if _covers(base, xs[i], ys[j]) and _covers(guard, xs[i], ys[j])}
    return _merge_cells(cells, xs, ys)


def attribute(changed_regions, footprint, dispatch_capture_seq, verify_frame_ref,
              peer_expectations=(), based_on_frame=None, baseline_regions=(),
              target_region=None):
    """Split ``changed_regions`` between the active actor and its peers.

    ``peer_expectations``: iterable of ``{"actor_id": str, "regions": [rect,
    ...]}`` (``footprint`` is accepted as one more region) for concurrent or
    prior peers whose effects may land between the two frames.

    Returns a dict with ``verdict``, ``own_regions``, ``spill_regions``,
    ``foreign_peer_regions`` (all peer-expected changes), ``contested_regions``
    (the part of those inside the footprint), ``foreign_by_actor`` and
    ``baseline_conflict_regions``.  Malformed regions other than the footprint
    raise ``ValueError``.
    """
    if based_on_frame is None:
        based_on_frame = dispatch_capture_seq
    seqs = (based_on_frame, dispatch_capture_seq, verify_frame_ref)
    if not is_rect(footprint):
        return _result(FOOTPRINT_MISSING, "footprint is not an integer [x, y, w, h] rect",
                       False, *seqs)
    if target_region is not None and not is_rect(target_region):
        raise ValueError("target_region %r is not an integer [x, y, w, h] rect" % (target_region,))
    if not all(_is_int(s) and s >= 0 for s in seqs):
        raise ValueError("frame sequence numbers must be non-negative ints")
    if based_on_frame > dispatch_capture_seq:
        return _result(STALE_FRAME, "based_on_frame is newer than dispatch_capture_seq",
                       False, *seqs)
    conflict = check_baseline(footprint, baseline_regions, target_region)
    if conflict:
        return _result(STALE_FRAME,
                       "foreign change since based_on_frame intersects the target",
                       False, *seqs, baseline_conflict_regions=conflict)
    if verify_frame_ref <= dispatch_capture_seq:
        return _result(STALE_FRAME, "verify_frame_ref is not newer than dispatch_capture_seq",
                       True, *seqs)

    fp = tuple(footprint)
    changed = _rects(changed_regions, "changed_regions")
    peers = _peer_list(peer_expectations)
    edges = changed + [fp] + [r for _, rects in peers for r in rects]
    if target_region is not None:
        edges.append(tuple(target_region))
    xs = sorted({v for r in edges for v in (r[0], r[0] + r[2])})
    ys = sorted({v for r in edges for v in (r[1], r[1] + r[3])})

    own, spill, foreign, contested = set(), set(), set(), set()
    by_actor = {actor_id: set() for actor_id, _ in peers}
    for j in range(len(ys) - 1):
        for i in range(len(xs) - 1):
            x, y = xs[i], ys[j]
            if not _covers(changed, x, y):
                continue
            cell = (j, i)
            inside = _covers((fp,), x, y)
            owners = [actor_id for actor_id, rects in peers if _covers(rects, x, y)]
            if owners:
                foreign.add(cell)
                for actor_id in owners:
                    by_actor[actor_id].add(cell)
                if inside:
                    contested.add(cell)
            elif inside:
                own.add(cell)
            else:
                spill.add(cell)

    regions = {
        "own_regions": _merge_cells(own, xs, ys),
        "spill_regions": _merge_cells(spill, xs, ys),
        "foreign_peer_regions": _merge_cells(foreign, xs, ys),
        "contested_regions": _merge_cells(contested, xs, ys),
        "foreign_by_actor": {actor_id: _merge_cells(cells, xs, ys)
                             for actor_id, cells in sorted(by_actor.items()) if cells},
    }
    target_spill = (target_region is not None and any(
        intersects(r, target_region) for r in regions["spill_regions"]))
    if contested:
        verdict, reason = FOREIGN_CHANGE_REFUSED, "peer-expected change inside the footprint"
    elif target_spill:
        verdict, reason = (FOREIGN_CHANGE_REFUSED,
                           "unattributed change intersects the target outside the footprint")
    elif not changed:
        verdict, reason = NO_VISIBLE_CHANGE, "zero changed regions"
    elif spill:
        verdict, reason = VERIFIED_WITH_SPILL, "unattributed change outside the footprint"
    elif not own:
        verdict, reason = VERIFIED_WITH_SPILL, "no change inside the footprint"
    else:
        verdict, reason = VERIFIED, ""
    out = _result(verdict, reason, True, *seqs, **regions)
    if verdict == VERIFIED:  # the negative-control law, checked on every success
        assert not out["spill_regions"] and not out["contested_regions"]
        assert out["own_regions"] and not out["baseline_conflict_regions"]
    return out
