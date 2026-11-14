# @module {"id":"wa.perceive.affordance","layer":2,"owner":"PERCEIVE","deps":["wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Deterministic CPU pixel affordance inference and safe vision STATE projection.

The algorithms and byte encodings implement the committed JQ1 A5 reference in
``docs/spec/jq1_a5_affordance_constellation_spec.md``. The test-local reference
remains independent: this module imports no tests, AX adapter, actor, or engine.
All pixels, intermediate arrays, and descriptors stay in memory. NumPy integer
arrays and Python integer geometry are the only numeric computation paths.

``reference_detect`` deliberately retains the jq1-a5-affordance-reference/v1
envelope, including provenance, abstention, calibration, and matching evidence.
``to_state`` is an explicit, nonauthorizing projection to wender.state/v1.
"""
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import cmp_to_key
import hashlib
import itertools
import json
import struct

import numpy as np

from ..state import schema


PPM = 1000000
EDGE_THRESHOLD = 128
PARTNERS = 8
PAIR_STEP_CSS = 2
K_CORNER, K_CENTER, K_CHECK, K_CLOSE, K_GLYPH = range(1, 6)
FP_DOMAIN = b"WAF5\x01"
PAIR_DOMAIN = b"WAP2\x01"
ABSTAIN = "ABSTAIN"
UNKNOWN = "INTERACTIVE_UNKNOWN"
MIN_SAMPLES, ACTION_PM, ROLE_PM = 20, 950, 900


def integer(value, low=None, high=None):
    if type(value) is not int:
        raise ValueError("Python int required (booleans/floats refused)")
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError("integer out of range")
    return value


def box4(box):
    if not isinstance(box, (tuple, list)) or len(box) != 4:
        raise ValueError("four half-open endpoints required")
    x0, y0, x1, y1 = (integer(x, -(2**31), 2**31 - 1) for x in box)
    if x1 < x0 or y1 < y0:
        raise ValueError("reversed box")
    return x0, y0, x1, y1


def normalize_box(box, dpr_ppm):
    """Outward endpoint rounding; tolerance elsewhere is one CSS pixel/edge."""
    integer(dpr_ppm, 1)
    x0, y0, x1, y1 = box4(box)
    left, top = x0 * PPM // dpr_ppm, y0 * PPM // dpr_ppm
    return (left, top, left if x0 == x1 else -(-x1 * PPM // dpr_ppm),
            top if y0 == y1 else -(-y1 * PPM // dpr_ppm))


def raster_box(box, dpr_ppm):
    integer(dpr_ppm, 1)
    x0, y0, x1, y1 = box4(box)
    left, top = x0 * dpr_ppm // PPM, y0 * dpr_ppm // PPM
    return (left, top, left if x0 == x1 else -(-x1 * dpr_ppm // PPM),
            top if y0 == y1 else -(-y1 * dpr_ppm // PPM))


def signed_round(n, denominator):
    integer(n)
    integer(denominator, 1)
    value = (2 * abs(n) + denominator) // (2 * denominator)
    return -value if n < 0 else value


def luma(rgb):
    if (not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8
            or rgb.ndim != 3 or rgb.shape[2] != 3 or min(rgb.shape[:2]) <= 0):
        raise ValueError("uint8[H,W,3] RGB required")
    wide = rgb.astype(np.int32)
    return (77 * wide[:, :, 0] + 150 * wide[:, :, 1]
            + 29 * wide[:, :, 2] + 128) >> 8


def sobel(y):
    """Replicate border, signed int32 Gx/Gy, L1 magnitude (no sqrt)."""
    p = np.pad(y.astype(np.int32), 1, mode="edge")
    gx = (-p[:-2, :-2] + p[:-2, 2:] - 2 * p[1:-1, :-2]
          + 2 * p[1:-1, 2:] - p[2:, :-2] + p[2:, 2:])
    gy = (-p[:-2, :-2] - 2 * p[:-2, 1:-1] - p[:-2, 2:]
          + p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:])
    return gx, gy, np.abs(gx) + np.abs(gy)


def component_boxes(edge):
    """8-connected ORIGINAL edges; row-major seeds; half-open bounds."""
    height, width = edge.shape
    seen = np.zeros(edge.shape, dtype=np.bool_)
    boxes = []
    for sy, sx in zip(*np.nonzero(edge)):
        sy, sx = int(sy), int(sx)
        if seen[sy, sx]:
            continue
        todo = deque([(sy, sx)])
        seen[sy, sx] = True
        x0 = x1 = sx
        y0 = y1 = sy
        while todo:
            cy, cx = todo.popleft()
            x0, x1 = min(x0, cx), max(x1, cx)
            y0, y1 = min(y0, cy), max(y1, cy)
            for dy, dx in itertools.product((-1, 0, 1), repeat=2):
                ny, nx = cy + dy, cx + dx
                if (0 <= ny < height and 0 <= nx < width
                        and edge[ny, nx] and not seen[ny, nx]):
                    seen[ny, nx] = True
                    todo.append((ny, nx))
        boxes.append((x0, y0, x1 + 1, y1 + 1))
    return sorted(boxes, key=lambda b: (b[1], b[0], b[3], b[2]))


def ink_boxes(edge):
    """Undo the one-device-pixel Sobel halo; clipped components abstain."""
    height, width = edge.shape
    return [(x0 + 1, y0 + 1, x1 - 1, y1 - 1)
            for x0, y0, x1, y1 in component_boxes(edge)
            if x0 > 0 and y0 > 0 and x1 < width and y1 < height
            and x1 - x0 >= 5 and y1 - y0 >= 5]


def contour_support(edge, box):
    """Top/right/bottom/left, 0..255; original edge within one device px.

    Corners contribute once to each incident side; no closed/dilated mask is
    accepted as evidence. Each side is tested separately.
    """
    x0, y0, x1, y1 = box4(box)
    height, width = edge.shape
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("nonempty in-frame box required")
    horizontal = lambda yy: int(np.any(edge[max(0, yy - 1):min(height, yy + 2),
                                             x0:x1], axis=0).sum()) * 255 // (x1 - x0)
    vertical = lambda xx: int(np.any(edge[y0:y1, max(0, xx - 1):min(width, xx + 2)],
                                           axis=1).sum()) * 255 // (y1 - y0)
    return (horizontal(y0), vertical(x1 - 1), horizontal(y1 - 1), vertical(x0))


def overlap(a, b):
    a, b = box4(a), box4(b)
    inter = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1]))
    union = ((a[2] - a[0]) * (a[3] - a[1])
             + (b[2] - b[0]) * (b[3] - b[1]) - inter)
    return inter, union


def iou_half(a, b):
    inter, union = overlap(a, b)
    return union > 0 and 2 * inter >= union


def label_context(box, components, dpr_ppm):
    """Three separate glyph-like components to the right on one baseline."""
    x0, y0, x1, y1 = normalize_box(box, dpr_ppm)
    near = []
    for other in components:
        a, b, c, d = normalize_box(other, dpr_ppm)
        if x1 + 3 <= a < c <= x1 + 80 and y0 <= b < d <= y1 and c - a <= 16:
            near.append((a, b, c, d))
    # Distinct nonoverlapping x intervals, with a <=2 CSS baseline spread.
    for baseline in sorted({n[3] for n in near}):
        run, end = 0, -2**31
        for a, b, c, d in sorted(near):
            if baseline <= d <= baseline + 2 and a >= end + 2:
                run, end = run + 1, c
        if run >= 3:
            return True
    return False


def checkbox_probe(box, components, dpr_ppm):
    x0, y0, x1, y1 = normalize_box(box, dpr_ppm)
    w, h = x1 - x0, y1 - y0
    return (8 <= w <= 24 and 8 <= h <= 24 and abs(w - h) <= 2
            and label_context(box, components, dpr_ppm))


def close_probe(y, glyph, parent, dpr_ppm):
    a, b, c, d = normalize_box(glyph, dpr_ppm)
    x0, y0, x1, y1 = normalize_box(parent, dpr_ppm)
    if not (5 <= c - a <= 20 and 5 <= d - b <= 20 and abs((c - a) - (d - b)) <= 2
            and x0 + 2 <= a and c <= x1 - 2 and y0 + 2 <= b
            and d <= y0 + min(24, (y1 - y0) // 2)
            and x1 - c <= min(24, (x1 - x0) // 2)):
        return False
    gx0, gy0, gx1, gy1 = glyph
    patch = y[gy0:gy1, gx0:gx1]
    lo, hi = int(patch.min()), int(patch.max())
    if hi - lo < 64:
        return False
    dark = patch <= (lo + hi) // 2
    h, w = dark.shape
    tolerance = max(1, (dpr_ppm + PPM - 1) // PPM)
    supports = [0, 0]
    off = 0
    for yy in range(h):
        left = signed_round(yy * (w - 1), max(1, h - 1))
        right = w - 1 - left
        for k, xx in enumerate((left, right)):
            supports[k] += int(np.any(dark[yy, max(0, xx - tolerance):min(w, xx + tolerance + 1)]))
        for xx in range(w):
            off += int(dark[yy, xx] and min(abs(xx - left), abs(xx - right)) > tolerance)
    return min(supports) * 4 >= h * 3 and off * 8 <= max(1, int(dark.sum()))


def propose(rgb, dpr_ppm=PPM):
    """Geometry and role HINTS only; no actionability or enabled-state oracle."""
    integer(dpr_ppm, 1)
    y = luma(rgb)
    edge = sobel(y)[2] >= EDGE_THRESHOLD
    components = ink_boxes(edge)
    rectangles = []
    for box in components:
        css = normalize_box(box, dpr_ppm)
        support = contour_support(edge, box)
        if (css[2] - css[0] >= 6 and css[3] - css[1] >= 6
                and min(support) >= 200):
            rectangles.append((box, support))
    # Prefer outer contours; suppress overlapping duplicate inner borders.
    rectangles.sort(key=lambda x: (-overlap(x[0], x[0])[0], x[0][1], x[0][0], x[0]))
    kept = []
    for box, support in rectangles:
        if not any(iou_half(box, k[0]) for k in kept):
            kept.append((box, support))
    out = []
    for box, support in kept:
        check = checkbox_probe(box, components, dpr_ppm)
        out.append({"box_device": list(box), "box_css": list(normalize_box(box, dpr_ppm)),
                    "kind": K_CHECK if check else K_CORNER,
                    "role_hint": "checkbox" if check else UNKNOWN,
                    "probe": "checkbox_context" if check else "closed_contour",
                    "support": list(support)})
    for glyph in components:
        if any(close_probe(y, glyph, parent, dpr_ppm) for parent, _ in kept):
            out.append({"box_device": list(glyph), "box_css": list(normalize_box(glyph, dpr_ppm)),
                        "kind": K_CLOSE, "role_hint": "button", "probe": "close_context",
                        "support": list(contour_support(edge, glyph))})
    out.sort(key=lambda r: (r["box_css"][1], r["box_css"][0], r["kind"], r["box_css"]))
    schema.dumps(out)
    return out


def descriptor48(rgb, box):
    """48 BYTES, with explicit nonuniform ranges; see the sibling spec."""
    y = luma(rgb)
    x0, y0, x1, y1 = box4(box)
    if not (0 <= x0 < x1 <= y.shape[1] and 0 <= y0 < y1 <= y.shape[0]):
        raise ValueError("descriptor box must be nonempty and in-frame")
    xs = x0 + ((2 * np.arange(16, dtype=np.int32) + 1) * (x1 - x0)) // 32
    ys = y0 + ((2 * np.arange(16, dtype=np.int32) + 1) * (y1 - y0)) // 32
    patch = y[ys[:, None], xs[None, :]]
    gx, gy, mag = sobel(patch)
    edge = mag >= EDGE_THRESHOLD
    means, densities = [], []
    for cy, cx in itertools.product(range(4), repeat=2):
        region = patch[cy * 4:(cy + 1) * 4, cx * 4:(cx + 1) * 4]
        means.append((int(region.sum()) // 16) >> 4)
        densities.append(15 * int(edge[cy * 4:(cy + 1) * 4, cx * 4:(cx + 1) * 4].sum()) // 16)
    directions = [0] * 8
    for yy, xx in zip(*np.nonzero(edge)):
        dx, dy = int(gx[yy, xx]), int(gy[yy, xx])
        if 2 * abs(dy) <= abs(dx):
            direction = 0 if dx >= 0 else 4
        elif 2 * abs(dx) <= abs(dy):
            direction = 2 if dy >= 0 else 6
        elif dx >= 0:
            direction = 1 if dy >= 0 else 7
        else:
            direction = 3 if dy >= 0 else 5
        directions[direction] += 1
    total = max(1, sum(directions))
    histogram = [255 * n // total for n in directions]
    supports = contour_support(sobel(y)[2] >= EDGE_THRESHOLD, box)
    tail = [int(patch.sum()) // 256, int(patch.max()) - int(patch.min()),
            255 * int((patch <= 127).sum()) // 256, min(255, 16 * (x1 - x0) // (y1 - y0))]
    return tuple(means + densities + histogram + list(supports) + tail)


def quantize48(descriptor):
    if len(descriptor) != 48:
        raise ValueError("d48 must have exactly 48 components")
    return tuple(integer(v, 0, 15 if i < 32 else 255) if i < 32
                 else integer(v, 0, 255) // 16 for i, v in enumerate(descriptor))


@dataclass(frozen=True)
class Anchor:
    kind: int
    x: int
    y: int
    d48: tuple

    def __post_init__(self):
        integer(self.kind, K_CORNER, K_GLYPH)
        integer(self.x, -(2**31), 2**31 - 1)
        integer(self.y, -(2**31), 2**31 - 1)
        if not isinstance(self.d48, tuple):
            raise ValueError("immutable descriptor tuple required")
        quantize48(self.d48)


def anchor_key(anchor):
    return anchor.y, anchor.x, anchor.kind, anchor.d48


def anchor_group(anchor):
    # Aliased glyph copies and identical template indices share ONE group.
    return anchor.kind, quantize48(anchor.d48)


def constellation_bytes(nodes):
    nodes = sorted(nodes, key=anchor_key)
    if len(nodes) != 5 or len({(n.x, n.y) for n in nodes}) != 5:
        raise ValueError("exactly five distinct spatial nodes required")
    origin_x, origin_y = min(n.x for n in nodes), min(n.y for n in nodes)
    out = bytearray(FP_DOMAIN + b"\x05")
    for n in nodes:
        dx, dy = n.x - origin_x, n.y - origin_y
        integer(dx, 0, 2**31 - 1)
        integer(dy, 0, 2**31 - 1)
        out.extend(struct.pack("<Bii", n.kind, dx, dy))
        out.extend(bytes(n.d48))
    return bytes(out)


def hash64(payload):
    """SHA-256's first 8 digest bytes, displayed in their original byte order."""
    return hashlib.sha256(payload).digest()[:8].hex()


def fp_struct(nodes):
    return hash64(constellation_bytes(nodes))


def box_constellation(rgb, box, dpr_ppm=PPM):
    """Half-open corner endpoints plus centre; clipped nominal 8-CSS patches.

    Landmark coordinates use nearest CSS rounding, independently of outward
    proposal bounds. Equal normalized geometry is NOT descriptor invariance.
    """
    integer(dpr_ppm, 1)
    x0, y0, x1, y1 = box4(box)
    height, width = rgb.shape[:2]
    radius = max(1, (4 * dpr_ppm + PPM - 1) // PPM)
    points = [(x0, y0, K_CORNER), (x1, y0, K_CORNER),
              ((x0 + x1) // 2, (y0 + y1) // 2, K_CENTER),
              (x0, y1, K_CORNER), (x1, y1, K_CORNER)]
    nodes = []
    for x, y, kind in points:
        patch = (max(0, x - radius), max(0, y - radius),
                 min(width, x + radius), min(height, y + radius))
        nodes.append(Anchor(kind, signed_round(x * PPM, dpr_ppm),
                            signed_round(y * PPM, dpr_ppm), descriptor48(rgb, patch)))
    constellation_bytes(nodes)  # Validate cardinality/normalization collapse.
    return tuple(sorted(nodes, key=anchor_key))


def extract_anchors(rgb, dpr_ppm=PPM):
    integer(dpr_ppm, 1)
    edge = sobel(luma(rgb))[2] >= EDGE_THRESHOLD
    return tuple(sorted((Anchor(K_GLYPH, signed_round((a + c - 1) * PPM, 2 * dpr_ppm),
                                signed_round((b + d - 1) * PPM, 2 * dpr_ppm),
                                descriptor48(rgb, (a, b, c, d)))
                         for a, b, c, d in ink_boxes(edge)), key=anchor_key))


def pair_bytes(first, second):
    dx = signed_round(second.x - first.x, PAIR_STEP_CSS)
    dy = signed_round(second.y - first.y, PAIR_STEP_CSS)
    integer(dx, -(2**31), 2**31 - 1)
    integer(dy, -(2**31), 2**31 - 1)
    return (PAIR_DOMAIN + bytes((first.kind, second.kind))
            + bytes(quantize48(first.d48)) + bytes(quantize48(second.d48))
            + struct.pack("<ii", dx, dy))


def bounded_pairs(nodes):
    nodes = sorted(set(nodes), key=anchor_key)
    out = []
    for first in nodes:
        partners = sorted((n for n in nodes if n != first), key=lambda n: (
            (n.x - first.x)**2 + (n.y - first.y)**2, anchor_key(n)))[:PARTNERS]
        out.extend((first, second, pair_bytes(first, second)) for second in partners)
    return out


def match_templates(templates, scene):
    """Hash-indexed offset voting, exact payload verification, group de-aliasing."""
    index = defaultdict(list)
    for template_id, nodes in sorted(templates.items()):
        if not isinstance(template_id, str) or not template_id:
            raise ValueError("nonempty template id required")
        for first, second, payload in bounded_pairs(nodes):
            index[hash64(payload)].append((template_id, first, payload))
    groups = defaultdict(set)
    for first, second, payload in bounded_pairs(scene):
        for template_id, anchor, stored in index.get(hash64(payload), ()):
            if stored != payload:  # A 64-bit hash is an index, never identity proof.
                continue
            dx = signed_round(first.x - anchor.x, PAIR_STEP_CSS)
            dy = signed_round(first.y - anchor.y, PAIR_STEP_CSS)
            groups[(template_id, dx, dy)].add(anchor_group(anchor))
    hypotheses = [{"template": tid, "offset_q": [dx, dy],
                   "distinct_groups": len(group), "votes": min(8, len(group))}
                  for (tid, dx, dy), group in groups.items()]
    hypotheses.sort(key=lambda h: (-h["votes"], h["template"], h["offset_q"][1], h["offset_q"][0]))
    result = {"best": hypotheses[0] if hypotheses else None,
              "tie": int(len(hypotheses) > 1 and hypotheses[0]["votes"] == hypotheses[1]["votes"]),
              "hypotheses": hypotheses}
    schema.dumps(result)
    return result


def smoothed(tp, fp):
    integer(tp, 0)
    integer(fp, 0)
    return 1000 * (tp + 1) // (tp + fp + 2)


def pool_calibration(bins):
    """Monotone SMOOTHED rates; merge raw counts, applying the prior once.

    Revisit left neighbours after each merge, including smoothing-induced
    cascades. Empty bins are unavailable and do not receive a usable prior.
    """
    counts = defaultdict(lambda: [0, 0])
    for score, tp, fp in bins:
        integer(score, 0, 8)
        integer(tp, 0)
        integer(fp, 0)
        counts[score][0] += tp
        counts[score][1] += fp
    blocks = [{"lo": score, "hi": score, "scores": [score], "tp": tp, "fp": fp}
              for score, (tp, fp) in sorted(counts.items()) if tp + fp]
    i = 0
    while i + 1 < len(blocks):
        a, b = blocks[i], blocks[i + 1]
        if (a["tp"] + 1) * (b["tp"] + b["fp"] + 2) > (b["tp"] + 1) * (a["tp"] + a["fp"] + 2):
            blocks[i:i + 2] = [{"lo": a["lo"], "hi": b["hi"], "scores": a["scores"] + b["scores"],
                                "tp": a["tp"] + b["tp"], "fp": a["fp"] + b["fp"]}]
            i = max(0, i - 1)
        else:
            i += 1
    for block in blocks:
        block["pm"] = smoothed(block["tp"], block["fp"])
        block["n"] = block["tp"] + block["fp"]
    schema.dumps(blocks)
    return blocks


def calibration_at(blocks, score):
    integer(score, 0, 8)
    return next((dict(b) for b in blocks if score in b["scores"]), None)


def calibration_examples(rows):
    """Frozen calibration split; repeated unit IDs cannot inflate sample count."""
    units = {}
    for row in rows:
        if row["split"] != "calibration":
            raise ValueError("test/training rows must not enter calibration")
        key = (row["family"], row["unit"])
        value = (integer(row["score"], 0, 8), integer(row["correct"], 0, 1))
        if key in units and units[key] != value:
            raise ValueError("conflicting repeated calibration unit")
        units[key] = value
    bins = defaultdict(lambda: [0, 0])
    for score, correct in units.values():
        bins[score][0 if correct else 1] += 1
    return [(score, *counts) for score, counts in sorted(bins.items())]


def decide(votes, action_fit, role_fit, role_hint=UNKNOWN, tie=0):
    integer(votes, 0, 8)
    integer(tie, 0, 1)
    if role_hint not in (UNKNOWN, "button", "checkbox"):
        raise ValueError("reference role hint must be button, checkbox, or unknown")
    action = calibration_at(action_fit, votes)
    role = calibration_at(role_fit, votes)
    sufficient = (votes >= 3 and not tie and action is not None
                  and action["n"] >= MIN_SAMPLES and action["pm"] >= ACTION_PM)
    if not sufficient:
        decision = ABSTAIN
    elif (role_hint == UNKNOWN or role is None or role["n"] < MIN_SAMPLES or role["pm"] < ROLE_PM):
        decision = UNKNOWN
    else:
        decision = role_hint
    return {"decision": decision, "votes": votes,
            "actionability_pm": None if action is None else action["pm"],
            "role_pm": None if role is None else role["pm"],
            "provenance": "vision", "level": None, "corroborated": 0,
            "enabled": None, "irreversible_admitted": 0}


# @anchor:wa.perceive.affordance.reference_detect
def reference_detect(rgb, dpr_ppm=PPM, templates=None, action_fit=(), role_fit=()):
    """Pixel proposals -> five-node reference fingerprint -> selective output."""
    output = []
    for i, proposal in enumerate(propose(rgb, dpr_ppm)):
        nodes = box_constellation(rgb, proposal["box_device"], dpr_ppm)
        matches = match_templates(templates or {}, nodes)
        votes = matches["best"]["votes"] if matches["best"] else 0
        decision = decide(votes, action_fit, role_fit, proposal["role_hint"], matches["tie"])
        output.append({"id": "p%04d" % i, "box": proposal["box_css"],
                       "role_hint": proposal["role_hint"], "probe": proposal["probe"],
                       "fp_struct": fp_struct(nodes), "match": matches, **decision})
    result = {"schema": "jq1-a5-affordance-reference/v1", "dpr_ppm": dpr_ppm, "proposals": output}
    schema.dumps(result)  # A detector envelope; STATE conversion is explicit in to_state.
    return result


def irreversible_allowed(evidence):
    """Proposed host-evidence boundary, not proof from self-asserted JSON.

    Only a trusted structured adapter may construct a corroborated record.
    Vision-origin records cannot upgrade themselves by supplying L0/L1 strings.
    """
    level = evidence.get("level")
    fingerprint = evidence.get("fp_exact" if level == "L0_EXACT" else "fp_stable")
    flag = lambda key, value: type(evidence.get(key)) is int and evidence[key] == value
    return bool(evidence.get("provenance") in ("cdp", "atspi", "godot")
                and flag("corroborated", 1) and level in ("L0_EXACT", "L1_STABLE")
                and isinstance(fingerprint, str) and len(fingerprint) == 16
                and all(c in "0123456789abcdef" for c in fingerprint)
                and flag("fresh", 1) and flag("tie", 0)
                and flag("enabled", 1) and flag("authorized", 1))


def one_to_one(predictions, truth):
    """Greedy descending rational IoU; ties by stable (prediction, truth) IDs.

    Deliberately NOT maximum-cardinality assignment. Roles never influence the
    geometric assignment. Each ID must be unique in its respective collection.
    """
    for rows in (predictions, truth):
        if len({r["id"] for r in rows}) != len(rows):
            raise ValueError("duplicate IDs")
        if any(not isinstance(r["id"], str) or not r["id"] for r in rows):
            raise ValueError("nonempty stable IDs required")
        for row in rows:
            box4(row["box"])
    edges = []
    for p in predictions:
        for t in truth:
            inter, union = overlap(p["box"], t["box"])
            if union > 0 and 2 * inter >= union:
                edges.append((p["id"], t["id"], inter, union))

    def compare(a, b):
        left, right = a[2] * b[3], b[2] * a[3]
        if left != right:
            return -1 if left > right else 1
        return (a[:2] > b[:2]) - (a[:2] < b[:2])

    used_p, used_t, selected = set(), set(), []
    for pid, tid, inter, union in sorted(edges, key=cmp_to_key(compare)):
        if pid not in used_p and tid not in used_t:
            used_p.add(pid)
            used_t.add(tid)
            selected.append({"prediction": pid, "truth": tid, "inter": inter, "union": union})
    return sorted(selected, key=lambda r: (r["prediction"], r["truth"]))


def fraction_pm(numerator, denominator):
    integer(numerator, 0)
    integer(denominator, 0)
    return None if denominator == 0 else 1000 * numerator // denominator


def evaluate(predictions, truth):
    """Hidden oracle labels enter ONLY here, after detection and selection."""
    selected = [p for p in predictions if p["decision"] != ABSTAIN]
    known = [p for p in selected if p["decision"] != UNKNOWN]
    by_truth = {t["id"]: t for t in truth}
    by_pred = {p["id"]: p for p in predictions}
    matched = one_to_one(selected, truth)
    proposals = one_to_one(predictions, truth)
    positive = sum(int(bool(t["actionable"])) for t in truth)
    correct = sum(int(bool(by_truth[m["truth"]]["actionable"])) for m in matched)
    recalled = sum(int(bool(by_truth[m["truth"]]["actionable"])) for m in proposals)
    role_tp = sum(int(bool(by_truth[m["truth"]]["actionable"])
                      and by_pred[m["prediction"]]["decision"] == by_truth[m["truth"]]["role"])
                  for m in matched if by_pred[m["prediction"]]["decision"] != UNKNOWN)
    result = {"matches": matched, "tp": correct, "fp": len(selected) - correct,
              "role_tp": role_tp, "role_fp": len(known) - role_tp,
              "actionable_truth": positive, "proposed": len(predictions), "selected": len(selected),
              "proposal_recall_pm": fraction_pm(recalled, positive),
              "class_agnostic_recall_pm": fraction_pm(correct, positive),
              "action_precision_pm": fraction_pm(correct, len(selected)),
              "role_conditioned_precision_pm": fraction_pm(role_tp, len(known)),
              "selection_coverage_pm": fraction_pm(len(selected), len(predictions)),
              "role_coverage_pm": fraction_pm(len(known), len(selected)),
              "selective_risk_pm": fraction_pm(len(selected) - correct, len(selected))}
    schema.dumps(result)
    return result


_VISION_NODE_DOMAIN = b"WAVS1\x01node\x00"
_VISION_EXACT_DOMAIN = b"WAVS1\x01exact\x00"
_VISION_STABLE_DOMAIN = b"WAVS1\x01stable\x00"


def _vision_digest(domain, value):
    """Domain-separated canonical vision content, never a trusted AX proof."""
    return hashlib.sha256(domain + schema.dumps(value).encode("utf-8")).hexdigest()


# @anchor:wa.perceive.affordance.to_state
def to_state(envelope, *, url, title, viewport, prune):
    """Project a detector envelope to a canonical, entirely nonactionable STATE.

    ``url`` and ``title`` are explicit strings or None. ``viewport`` is None or
    a mapping of string keys to Python integers (typically CSS width/height).
    ``prune`` supplies at least a string ``name`` and integer ``version``. These
    metadata inputs and the envelope are copied, never mutated or retained by
    alias. Proposal boxes are half-open CSS endpoints; STATE boxes are xywh.

    All proposals, including abstentions, remain visible as flat root nodes.
    Only a selected button/checkbox decision supplies a known STATE role;
    other roles are INTERACTIVE_UNKNOWN. No pixel reading establishes enabled
    state, names, structured ancestry, or permission, so names/states are empty
    and actionability is always false. Decision evidence stays in the original
    detector envelope and is not smuggled into unsupported STATE node fields.

    Node IDs are the first 16 hex characters of SHA-256 over WAVS1/version-1
    node-domain bytes plus the canonical box/fp_struct/probe/role_hint record.
    They ignore proposal order and calibrated decisions, and do not promise
    cross-DPI or motion tracking invariance. Duplicate identities or truncated
    digest collisions are refused rather than silently aliasing nodes.

    The legacy required ax_sha256 slot hashes the canonically sorted detector
    envelope under the WAVS1 exact domain; ax_stable_sha256 hashes the ordered
    vision identity records under the WAVS1 stable domain. These are vision
    serialization/change-tracking digests, not AX fingerprints or action proof.
    No fp_exact, fp_stable, L0_EXACT, or L1_STABLE evidence is constructed.
    """
    if not isinstance(envelope, dict) or envelope.get("schema") != "jq1-a5-affordance-reference/v1":
        raise ValueError("jq1-a5-affordance-reference/v1 detector envelope required")
    integer(envelope.get("dpr_ppm"), 1)
    if not isinstance(envelope.get("proposals"), list):
        raise ValueError("detector proposals must be a list")
    if not all(value is None or isinstance(value, str) for value in (url, title)):
        raise ValueError("source url/title must be strings or None")
    if viewport is not None:
        if not isinstance(viewport, dict) or not all(isinstance(key, str) for key in viewport):
            raise ValueError("viewport must be None or a string-keyed integer mapping")
        for value in viewport.values():
            integer(value)
    if not isinstance(prune, dict) or not isinstance(prune.get("name"), str):
        raise ValueError("prune metadata requires a string name and integer version")
    integer(prune.get("version"))

    # Canonical serialization validates the entire supplied content, including
    # nested dictionary keys, before JSON copying can normalize any keys.
    snapshot = json.loads(schema.dumps({"envelope": envelope, "viewport": viewport, "prune": prune}))
    detector = snapshot["envelope"]
    records = []
    proposal_ids, node_ids = set(), set()
    for proposal in detector["proposals"]:
        if not isinstance(proposal, dict):
            raise ValueError("each detector proposal must be an object")
        proposal_id = proposal.get("id")
        if not isinstance(proposal_id, str) or not proposal_id or proposal_id in proposal_ids:
            raise ValueError("unique nonempty detector proposal ids required")
        proposal_ids.add(proposal_id)
        endpoints = box4(proposal.get("box"))
        role_hint, decision = proposal.get("role_hint"), proposal.get("decision")
        if role_hint not in (UNKNOWN, "button", "checkbox") or decision not in (ABSTAIN, UNKNOWN, "button", "checkbox"):
            raise ValueError("invalid detector role hint or decision")
        fingerprint, probe = proposal.get("fp_struct"), proposal.get("probe")
        if (not isinstance(fingerprint, str) or len(fingerprint) != 16
                or any(c not in "0123456789abcdef" for c in fingerprint)):
            raise ValueError("vision fp_struct must be 16 lowercase hex characters")
        if not isinstance(probe, str) or not probe:
            raise ValueError("nonempty detector probe required")
        if (proposal.get("provenance") != "vision" or proposal.get("enabled") is not None
                or proposal.get("level") in ("L0_EXACT", "L1_STABLE")):
            raise ValueError("uncorroborated vision provenance with unknown enabled state required")
        integer(proposal.get("corroborated"), 0, 0)
        integer(proposal.get("irreversible_admitted"), 0, 0)
        identity = {"box": list(endpoints), "fp_struct": fingerprint,
                    "probe": probe, "role_hint": role_hint}
        node_id = _vision_digest(_VISION_NODE_DOMAIN, identity)[:16]
        if node_id in node_ids:
            raise ValueError("duplicate vision identity or node digest collision")
        node_ids.add(node_id)
        records.append((identity, proposal, node_id))

    records.sort(key=lambda record: (record[0]["box"][1], record[0]["box"][0],
                                    record[0]["box"][3], record[0]["box"][2],
                                    record[0]["role_hint"], record[0]["probe"],
                                    record[0]["fp_struct"]))
    nodes, boxes = [], {}
    for ordinal, (identity, proposal, node_id) in enumerate(records):
        x0, y0, x1, y1 = identity["box"]
        boxes[node_id] = [x0, y0, x1 - x0, y1 - y0]
        nodes.append({"id": node_id, "role": proposal["decision"]
                      if proposal["decision"] in ("button", "checkbox") else UNKNOWN,
                      "name": "", "depth": 0, "ordinal": ordinal, "attrs": []})
    detector["proposals"] = [proposal for _, proposal, _ in records]
    stable_content = {"schema": detector["schema"], "dpr_ppm": detector["dpr_ppm"],
                      "identities": [identity for identity, _, _ in records]}
    state = schema.build_state(
        nodes, kind="vision", url=url, title=title,
        ax_sha256=_vision_digest(_VISION_EXACT_DOMAIN, detector),
        ax_stable_sha256=_vision_digest(_VISION_STABLE_DOMAIN, stable_content),
        prune=snapshot["prune"], raw_count=len(records), boxes=boxes,
        viewport=snapshot["viewport"], actionable_roles=frozenset())
    schema.dumps(state)
    return state


