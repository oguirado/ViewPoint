# @module {"id":"wa.perceive.transform","layer":1,"owner":"PERCEIVE","deps":["wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Integer projection and calibration, ported from fastexec/bench/osc2.py.

Boxes enter as exclusive-end xywh and use R10 closed intervals internally.
No input values, text, browser identities, or pixels are retained.
"""
from dataclasses import asdict, dataclass
from ..state import schema

SCHEMA_ID = "vqa.coord-transform/v1"
DEFAULT_PPM = 1_000_000
HALF_PPM = 500_000
MIN_PPM = 100_000
MAX_PPM = 16_000_000
MAX_COORD = 1_048_576
IOU_MIN_PPM = 500_000
IOU_MIN_PROMILLE = 500
PROMILLE_DEN = 1_000
MIN_SUPPORT = 3
MAX_RESIDUAL_PX = 3
MAX_RESIDUAL_SQ = 9
DX_MIN, DX_MAX, DX_STEP = -10, 10, 2
DY_MIN, DY_MAX, DY_STEP = 0, 120, 4
REFINE_ROUNDS = 3


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


# @anchor:wa.perceive.transform.exact_int
def exact_int(value, low=None, high=None, name="value"):
    if type(value) is not int or (low is not None and value < low) or (high is not None and value > high):
        raise ValueError("invalid " + name)
    return value


# @anchor:wa.perceive.transform.compose_scale_ppm
def compose_scale_ppm(dpr_ppm=DEFAULT_PPM, zoom_ppm=DEFAULT_PPM):
    exact_int(dpr_ppm, MIN_PPM, MAX_PPM, "dpr_ppm")
    exact_int(zoom_ppm, MIN_PPM, MAX_PPM, "zoom_ppm")
    return exact_int((dpr_ppm * zoom_ppm + HALF_PPM) // DEFAULT_PPM, MIN_PPM, MAX_PPM, "scale_ppm")


# @anchor:wa.perceive.transform.project_coord
def project_coord(css_coord, scale_ppm=DEFAULT_PPM, d=0):
    exact_int(css_coord, -MAX_COORD, MAX_COORD)
    exact_int(scale_ppm, MIN_PPM, MAX_PPM, "scale_ppm")
    exact_int(d, -MAX_COORD, MAX_COORD, "offset")
    return (css_coord * scale_ppm + HALF_PPM) // DEFAULT_PPM + d


# @anchor:wa.perceive.transform.xywh_to_closed
def xywh_to_closed(box):
    if isinstance(box, dict) and "bbox" in box:
        box = box["bbox"]
    if isinstance(box, (list, tuple)):
        vals = box
    else:
        vals = [_get(box, k) for k in ("x", "y", "w", "h")]
    if len(vals) != 4 or any(type(v) is not int for v in vals):
        return None
    x, y, w, h = vals
    if w <= 0 or h <= 0:
        return None
    return x, y, x + w - 1, y + h - 1


# @anchor:wa.perceive.transform.closed_to_xywh
def closed_to_xywh(closed_box):
    if not isinstance(closed_box, (list, tuple)) or len(closed_box) != 4:
        return None
    if any(type(v) is not int for v in closed_box):
        return None
    x0, y0, x1, y1 = closed_box
    return [x0, y0, x1 - x0 + 1, y1 - y0 + 1] if x1 >= x0 and y1 >= y0 else None


# @anchor:wa.perceive.transform.css_box_to_closed_fb
def css_box_to_closed_fb(box, scale_ppm=DEFAULT_PPM, dx=0, dy=0):
    c = xywh_to_closed(box)
    if c is None or dx is None or dy is None:
        return None
    x0, y0 = project_coord(c[0], scale_ppm, dx), project_coord(c[1], scale_ppm, dy)
    xe, ye = project_coord(c[2] + 1, scale_ppm, dx), project_coord(c[3] + 1, scale_ppm, dy)
    return (x0, y0, xe - 1, ye - 1) if xe > x0 and ye > y0 else None


# @anchor:wa.perceive.transform.translated_box
def translated_box(box, transform):
    if transform is None or _get(transform, "status", "certified") != "certified":
        return None
    dx, dy = _get(transform, "dx"), _get(transform, "dy")
    if type(dx) is not int or type(dy) is not int:
        return None
    return closed_to_xywh(css_box_to_closed_fb(box, _get(transform, "scale_ppm", DEFAULT_PPM), dx, dy))


# @anchor:wa.perceive.transform.closed_overlap_and_union
def closed_overlap_and_union(box_a, box_b):
    if closed_to_xywh(box_a) is None or closed_to_xywh(box_b) is None:
        return 0, 0, 0, 0
    ax, ay, ae, af = box_a
    bx, by, be, bf = box_b
    ra, rb = (ae - ax + 1) * (af - ay + 1), (be - bx + 1) * (bf - by + 1)
    ov = max(0, min(ae, be) - max(ax, bx) + 1) * max(0, min(af, bf) - max(ay, by) + 1)
    return ov, ra + rb - ov, ra, rb


# @anchor:wa.perceive.transform.CoordTransform
@dataclass(frozen=True)
class CoordTransform:
    schema: str = SCHEMA_ID
    scale_ppm: int = DEFAULT_PPM
    dx: int | None = None
    dy: int | None = None
    dxdy_space: str = "frame-px"
    support: int = 0
    residual_px_num: int | None = None
    residual_px_den: int = 1
    iou_min_ppm: int = IOU_MIN_PPM
    method: str = "grid-median-hungarian"
    viewport_id: str = "default"
    chrome_id: str = "default"
    epoch: int = 0
    status: str = "absent"
    reason: str | None = None
    source: str = "estimated"

    # @anchor:wa.perceive.transform.CoordTransform.__post_init__
    def __post_init__(self):
        exact_int(self.scale_ppm, MIN_PPM, MAX_PPM)
        exact_int(self.support, 0)
        exact_int(self.epoch, 0)
        if self.status not in ("certified", "absent"):
            raise ValueError("invalid transform status")
        if self.status == "absent":
            if self.dx is not None or self.dy is not None:
                raise ValueError("absent transform offsets")
        else:
            exact_int(self.dx, -MAX_COORD, MAX_COORD)
            exact_int(self.dy, -MAX_COORD, MAX_COORD)
            exact_int(self.support, MIN_SUPPORT)
            exact_int(self.residual_px_num, 0, MAX_RESIDUAL_SQ)
        if self.residual_px_num is not None:
            exact_int(self.residual_px_num, 0)
        if self.residual_px_den != 1 or type(self.residual_px_den) is not int:
            raise ValueError("invalid residual denominator")
        schema.dumps(asdict(self))

    # @anchor:wa.perceive.transform.CoordTransform.to_dict
    def to_dict(self):
        result = asdict(self)
        schema.dumps(result)
        return result


def _median_round_half_up(values):
    values = sorted(values)
    k = len(values)
    return values[k // 2] if k % 2 else (values[k // 2 - 1] + values[k // 2] + 1) // 2


def _hungarian(weights):
    """Maximum-weight rectangular assignment; add one dummy column per row."""
    rows = len(weights)
    cols_real = len(weights[0]) if rows else 0
    cols = cols_real + rows
    u, v, owner, way = [0] * (rows + 1), [0] * (cols + 1), [0] * (cols + 1), [0] * (cols + 1)
    for i in range(1, rows + 1):
        owner[0] = i
        j0 = 0
        best, used = [10**12] * (cols + 1), [False] * (cols + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = owner[j0], 10**12, 0
            for j in range(1, cols + 1):
                if used[j]:
                    continue
                weight = weights[i0 - 1][j - 1] if j <= cols_real else 0
                cost = -weight - u[i0] - v[j]
                if cost < best[j]:
                    best[j], way[j] = cost, j0
                if best[j] < delta:
                    delta, j1 = best[j], j
            for j in range(cols + 1):
                if used[j]:
                    u[owner[j]] += delta
                    v[j] -= delta
                else:
                    best[j] -= delta
            j0 = j1
            if owner[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            owner[j0] = owner[j1]
            j0 = j1
    pairs = sorted((owner[j] - 1, j - 1) for j in range(1, cols_real + 1)
                   if owner[j] and weights[owner[j] - 1][j - 1] > 0)
    return pairs, u, v


def _one_to_one_int(mark_boxes, node_boxes, scale_ppm, dx, dy, optimal=False):
    evidence, weights = {}, []
    for i, nb in enumerate(node_boxes):
        projected = css_box_to_closed_fb(nb, scale_ppm, dx, dy)
        row = []
        for j, mb in enumerate(mark_boxes):
            ov, union, _, _ = closed_overlap_and_union(projected, xywh_to_closed(mb))
            w = DEFAULT_PPM * ov // union if union > 0 and 2 * ov >= union else 0
            row.append(w)
            evidence[i, j] = (w, ov, union)
        weights.append(row)
    if optimal:
        pairs = _hungarian(weights)[0]
    else:
        pairs, seen_i, seen_j = [], set(), set()
        for _, i, j in sorted((-e[0], i, j) for (i, j), e in evidence.items() if e[0]):
            if i not in seen_i and j not in seen_j:
                pairs.append((i, j))
                seen_i.add(i)
                seen_j.add(j)
    return [(i, j, *evidence[i, j]) for i, j in pairs]


def _bounds(bounds):
    if not isinstance(bounds, (tuple, list)) or len(bounds) != 3:
        raise ValueError("invalid search bounds")
    lo, hi, step = bounds
    exact_int(lo, -MAX_COORD, MAX_COORD)
    exact_int(hi, lo, MAX_COORD)
    exact_int(step, 1, MAX_COORD)
    return lo, hi, step


# @anchor:wa.perceive.transform.estimate_transform
def estimate_transform(marks, nodes, *, scale_ppm=DEFAULT_PPM, dpr_ppm=None, zoom_ppm=None,
                       viewport_id="default", chrome_id="default", epoch=0,
                       dx_bounds=(DX_MIN, DX_MAX, DX_STEP), dy_bounds=(DY_MIN, DY_MAX, DY_STEP), cache=None):
    if dpr_ppm is not None or zoom_ppm is not None:
        scale_ppm = compose_scale_ppm(DEFAULT_PPM if dpr_ppm is None else dpr_ppm,
                                      DEFAULT_PPM if zoom_ppm is None else zoom_ppm)
    exact_int(scale_ppm, MIN_PPM, MAX_PPM)
    exact_int(epoch, 0)
    xl, xh, xs = _bounds(dx_bounds)
    yl, yh, ys = _bounds(dy_bounds)
    key = (viewport_id, chrome_id, scale_ppm, epoch)
    if isinstance(cache, dict) and key in cache:
        return dict(cache[key])
    ns = sorted((n for n in nodes if _get(n, "actionable") is True
                 and _get(n, "states", {}).get("visible") in (True, "true")
                 and xywh_to_closed(_get(n, "box")) is not None), key=lambda n: _get(n, "id"))
    ms = sorted((m for m in marks if xywh_to_closed(m) is not None), key=lambda m: _get(m, "id"))
    mb, nb = ms, [_get(n, "box") for n in ns]
    support, residual, reason, dx, dy = 0, None, "insufficient_candidates", None, None
    if min(len(ns), len(ms)) >= MIN_SUPPORT:
        grid = (((len(p), sum(e[2] for e in p), -abs(x), -y), x, y)
                for y in range(yl, yh + 1, ys) for x in range(xl, xh + 1, xs)
                for p in [_one_to_one_int(mb, nb, scale_ppm, x, y)])
        _, dx, dy = max(grid, key=lambda entry: entry[0])
        for _ in range(REFINE_ROUNDS):
            pairs = _one_to_one_int(mb, nb, scale_ppm, dx, dy, True)
            if len(pairs) < MIN_SUPPORT:
                break
            origins = [(xywh_to_closed(mb[j]), css_box_to_closed_fb(nb[i], scale_ppm)) for i, j, *_ in pairs]
            nx = _median_round_half_up([m[0] - n[0] for m, n in origins])
            ny = _median_round_half_up([m[1] - n[1] for m, n in origins])
            if (nx, ny) == (dx, dy):
                break
            dx, dy = nx, ny
        pairs = _one_to_one_int(mb, nb, scale_ppm, dx, dy, True)
        support = len(pairs)
        r2 = []
        for i, j, *_ in pairs:
            m, n = xywh_to_closed(mb[j]), css_box_to_closed_fb(nb[i], scale_ppm, dx, dy)
            r2.append((m[0] - n[0])**2 + (m[1] - n[1])**2)
        residual = _median_round_half_up(r2) if r2 else None
        reason = ("support_below_3" if support < MIN_SUPPORT else
                  "residual_above_3px" if residual is None or residual > MAX_RESIDUAL_SQ else
                  "offset_out_of_bounds" if not (xl <= dx <= xh and yl <= dy <= yh) else None)
    certified = reason is None
    result = CoordTransform(scale_ppm=scale_ppm, dx=dx if certified else None,
                            dy=dy if certified else None, support=support, residual_px_num=residual,
                            viewport_id=viewport_id, chrome_id=chrome_id, epoch=epoch,
                            status="certified" if certified else "absent", reason=reason).to_dict()
    if isinstance(cache, dict):
        cache[key] = dict(result)
    return result
