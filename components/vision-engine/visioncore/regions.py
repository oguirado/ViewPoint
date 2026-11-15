# @module {"id":"vc.s4","layer":2,"stage":"S4","owner":"CORE","deps":["vc.ids","vc.layout","vc.errors"],"inputs":"tile records of one frame (cls,flags,resid,mv,edge)","outputs":"vqa.core-region/v1 records, adjacency, vqa.core-link/v1","invariants":["integer-only","deterministic","order-independent"],"anti_uses":["float thresholds","per-engine branches"],"oracle":"tests/test_regions.py","book":"HB-VC-s4-regions"}
"""@module vc.s4 L2 - S4 region islands, adjacency graph, t-1 -> t correspondence (SPEC_CORE §5).

Groups: G_CHG {CHANGED,FLICKER,DRIFT}, G_MOV by equal mv, G_FRZ {FROZEN}. Islands are 8-connected within a group;
the region key is the min Morton of the island (I5), so output never depends on traversal order.
"""
import numpy as np

from . import ids, layout
from .errors import VqaError

MATCH_OVERLAP_PPM = 500000
SHADE_ONLY_PPM = 900000
FLAG_EDGE = 1 << 0
FLAG_TEXTURE = 1 << 1
FLAG_SHADE_ONLY = 1 << 2
FLAG_UI_OCC = 1 << 4
FLAG_ACTOR_OCC = 1 << 5

_C_CHANGED, _C_MOVED, _C_FLICKER, _C_FROZEN, _C_DRIFT = 2, 3, 4, 5, 6
_KINDS = ("EDGE", "TEXTURE", "SMOOTH", "MIXED")
_NB8 = tuple((dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dx, dy) != (0, 0))


_DIRTY_CLS = (_C_CHANGED, _C_MOVED, _C_FLICKER, _C_FROZEN, _C_DRIFT)


def _compact_v(a):
    """Vectorised layout._compact: the even bits of a (u16 range) packed into 8 bits."""
    a = a & 0x5555
    a = (a | (a >> 1)) & 0x3333
    a = (a | (a >> 2)) & 0x0F0F
    return (a | (a >> 4)) & 0x00FF


def _group(cls, dx, dy):
    if cls in (_C_CHANGED, _C_FLICKER, _C_DRIFT):
        return ("CHG",)
    if cls == _C_MOVED:
        return ("MOV", dx, dy)
    if cls == _C_FROZEN:
        return ("FRZ",)
    return None


def _kind_of(flags, edge, s):
    if flags & FLAG_TEXTURE:
        return 1
    if flags & FLAG_EDGE:
        return 0
    return 2 if edge * s * 8 < 255 else 3


def _runs(sorted_m):
    runs = []
    for m in sorted_m:
        if runs and runs[-1][1] + 1 == m:
            runs[-1][1] = m
        else:
            runs.append([m, m])
    return runs


def _islands(cells):
    """cells: dict (tx,ty)->morton. -> list of lists of (tx,ty); label = min Morton by union-by-min-Morton."""
    parent = {c: c for c in cells}

    def find(c):
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    for c in sorted(cells, key=lambda k: (k[1], k[0])):  # raster order
        for dx, dy in _NB8:
            n = (c[0] + dx, c[1] + dy)
            if n in cells:
                a, b = find(c), find(n)
                if a != b:
                    if cells[a] <= cells[b]:
                        parent[b] = a
                    else:
                        parent[a] = b
    comps = {}
    for c in cells:
        comps.setdefault(find(c), []).append(c)
    return list(comps.values())


def regions_of(tiles, W, H, frame):  # @anchor:vc.s4.regions
    """-> list of vqa.core-region/v1 dicts sorted by min Morton. tiles: structured array with layout.TILE_FIELDS."""
    s, gw, gh = layout.grid_params(W, H)
    ident = layout.grid_ident(W, H)
    groups = {}
    info = {}
    # RPERF prefilter: only dirty classes can join a region; range check stays vectorised over every morton
    mortons = tiles["morton"].astype(np.int64)
    txs, tys = _compact_v(mortons), _compact_v(mortons >> 1)
    out_of_grid = np.nonzero((txs >= gw) | (tys >= gh))[0]
    if len(out_of_grid):
        raise VqaError("E_VC_ID_RANGE", {"morton": int(mortons[out_of_grid[0]])})
    for i in np.nonzero(np.isin(tiles["cls"], _DIRTY_CLS))[0]:
        t = tiles[i]
        cls = int(t["cls"])
        g = _group(cls, int(t["mv_dx"]), int(t["mv_dy"]))
        m = int(t["morton"])
        tx, ty = layout.unmorton(m)
        groups.setdefault(g, {})[(tx, ty)] = m
        info[m] = (cls, int(t["flags"]), int(t["resid"]), int(t["edge"]))
    out = []
    for g, cells in groups.items():
        for isl in _islands(cells):
            ms = sorted(cells[c] for c in isl)
            hist = [0] * 7
            kinds = [0] * 4
            rmax = rsum = esum = shade = 0
            occ_ui = occ_ac = False
            for m in ms:
                cls, fl, res, edge = info[m]
                hist[cls] += 1
                kinds[_kind_of(fl, edge, s)] += 1
                rmax = max(rmax, res)
                rsum += res
                esum += edge
                shade += 1 if fl & FLAG_SHADE_ONLY else 0
                occ_ui = occ_ui or bool(fl & FLAG_UI_OCC)
                occ_ac = occ_ac or bool(fl & FLAG_ACTOR_OCC)
            n = len(ms)
            xs = [c[0] for c in isl]
            ys = [c[1] for c in isl]
            tx0, ty0, tx1, ty1 = min(xs), min(ys), max(xs), max(ys)
            x, y = tx0 * s, ty0 * s
            bbox_px = [x, y, min(W, (tx1 + 1) * s) - x, min(H, (ty1 + 1) * s) - y]
            k, b0, b1 = _aligned(tx0, ty0, tx1, ty1)
            kmax = max(kinds)
            rec = {
                "schema": "vqa.core-region/v1",
                "id": ids.region_id(frame, ms[0], W, H),
                "frame": frame, "grid": ident, "group": g[0],
                "cls": layout.CLS[hist.index(max(hist))],
                "cls_hist": hist, "n_tiles": n,
                "bbox_t": [tx0, ty0, tx1, ty1], "bbox_px": bbox_px,
                "mv": [g[1], g[2]] if g[0] == "MOV" else [0, 0],
                "r_max": rmax, "r_sum": rsum,
                "edge_ppm": 1000000 * esum // (255 * n),
                "kind": _KINDS[kinds.index(kmax)],
                "shade_only": 1000000 * shade >= SHADE_ONLY_PPM * n,
                "occ": [o for o, f in (("ui", occ_ui), ("actor", occ_ac)) if f],
                "block": [k, "%04x" % b0, "%04x" % b1],
                "spans": [["%04x" % a, "%04x" % b] for a, b in _runs(ms)],
            }
            out.append(rec)
    out.sort(key=lambda r: int(r["id"].split(":")[1], 16))
    return out


def _aligned(tx0, ty0, tx1, ty1):
    k = 0
    while (tx0 >> k) != (tx1 >> k) or (ty0 >> k) != (ty1 >> k):
        k += 1
    m0 = layout.morton((tx0 >> k) << k, (ty0 >> k) << k)
    return k, m0, m0 + 4 ** k - 1


def region_tiles(rec):
    """Expand a region record's spans into a sorted list of Morton codes."""
    return [m for a, b in rec["spans"] for m in range(int(a, 16), int(b, 16) + 1)]


def adjacency(regions):  # @anchor:vc.s4.adjacency
    """Untyped region adjacency graph of ONE frame: sorted [[id_a, id_b, contacts]], a < b.
    contacts = number of unordered 8-neighbour tile pairs belonging to the two regions. Excluded from pack/vmem (I15)."""
    owner = {}
    for r in regions:
        for m in region_tiles(r):
            owner[layout.unmorton(m)] = r["id"]
    cnt = {}
    for (tx, ty), a in owner.items():
        for dx, dy in ((1, -1), (1, 0), (1, 1), (0, 1)):  # half neighbourhood: each pair once
            b = owner.get((tx + dx, ty + dy))
            if b is not None and b != a:
                key = (a, b) if a < b else (b, a)
                cnt[key] = cnt.get(key, 0) + 1
    return [[a, b, c] for (a, b), c in sorted(cnt.items())]


def mv_in_moore(regions, s):
    """True iff every MOVED region's displacement keeps its source inside the 3x3 Moore neighbourhood (|mv| <= s)."""
    return all(max(abs(r["mv"][0]), abs(r["mv"][1])) <= s for r in regions)


def match_regions(prev, cur, global_mv=(0, 0), s=16):  # @anchor:vc.s4.match
    """Greedy one-to-one correspondence prev (t-1) -> cur (t), deterministic. -> vqa.core-link/v1 dict."""
    if prev and cur and prev[0]["grid"] != cur[0]["grid"]:
        raise VqaError("E_VC_GRID_MISMATCH", {"prev": prev[0]["grid"], "cur": cur[0]["grid"]})
    gsx = (global_mv[0] + s // 2) // s
    gsy = (global_mv[1] + s // 2) // s
    owner = {}
    for r in cur:
        for m in region_tiles(r):
            owner[m] = r["id"]
    size = {r["id"]: r["n_tiles"] for r in prev + cur}
    edges = {}
    for a in prev:
        if a["group"] == "MOV":  # a MOVED region travels by its own displacement (px), floor (I4)
            shx, shy = (a["mv"][0] + s // 2) // s, (a["mv"][1] + s // 2) // s
        else:
            shx, shy = gsx, gsy
        for m in region_tiles(a):
            tx, ty = layout.unmorton(m)
            tx, ty = tx + shx, ty + shy
            if not (0 <= tx <= layout.AXIS_MAX and 0 <= ty <= layout.AXIS_MAX):
                continue
            b = owner.get(layout.morton(tx, ty))
            if b is not None:
                edges[(a["id"], b)] = edges.get((a["id"], b), 0) + 1
    kept = [(ov, a, b) for (a, b), ov in edges.items()
            if 1000000 * ov >= MATCH_OVERLAP_PPM * min(size[a], size[b])]
    kept.sort(key=lambda e: (-e[0], e[1], e[2]))
    ma, mb, links, rest = set(), set(), [], []
    for ov, a, b in kept:
        if a in ma or b in mb:
            rest.append((a, b))
        else:
            ma.add(a)
            mb.add(b)
            links.append([a, b, ov])
    split = sorted([a, b] for a, b in rest if a in ma)
    merge = sorted([a, b] for a, b in rest if a not in ma)
    frame = cur[0]["frame"] if cur else (prev[0]["frame"] + 1 if prev else 0)
    return {"schema": "vqa.core-link/v1", "frame": frame, "links": sorted(links), "split": split, "merge": merge}
