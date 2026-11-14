"""Independent A5 reference v1 and production equivalence regressions.

Only stdlib and NumPy; RGB frames are constructed and retained in RAM. The
production imports below supply serialization and narrowly isolated live probes.
Algorithms/thresholds/encodings stay test-local; production is compared against
them and literal vectors, never imported by or substituted for the reference.
"""
import ast
from collections import defaultdict, deque
from dataclasses import dataclass
from functools import cmp_to_key
import hashlib
import itertools
import json
from pathlib import Path
import struct
import unittest

import numpy as np

from wender.act import cdp_executor
from wender.perceive import affordance as production, ax, vision_bridge
from wender.perceive.vision_adapter import Frame, fuse_regions
from wender.state import schema


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
    schema.dumps(result)  # Does NOT imply validate_state accepts a vision STATE.
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


# RAM fixtures. These are rasterized afresh, not transformed detection results.
def canvas(width=160, height=100):
    return np.full((height, width, 3), 248, dtype=np.uint8)


def paint_rect(rgb, box, thickness=2, color=16):
    x0, y0, x1, y1 = box4(box)
    rgb[y0:y0 + thickness, x0:x1] = color
    rgb[y1 - thickness:y1, x0:x1] = color
    rgb[y0:y1, x0:x0 + thickness] = color
    rgb[y0:y1, x1 - thickness:x1] = color


def paint_x(rgb, box):
    x0, y0, x1, y1 = box
    for yy in range(y1 - y0):
        xx = signed_round(yy * (x1 - x0 - 1), y1 - y0 - 1)
        rgb[y0 + yy, x0 + xx] = 16
        rgb[y0 + yy, x1 - 1 - xx] = 16


GLYPHS = (
    ("101", "101", "111", "101", "101"),
    ("111", "100", "111", "100", "111"),
    ("111", "010", "010", "010", "010"),
    ("100", "100", "100", "100", "111"),
    ("111", "101", "111", "100", "100"),
)


def paint_glyph(rgb, x, y, which=0, scale=2):
    for row, bits in enumerate(GLYPHS[which]):
        for col, bit in enumerate(bits):
            if bit == "1":
                rgb[y + row * scale:y + (row + 1) * scale,
                    x + col * scale:x + (col + 1) * scale] = 16


def glyph_scene(repeated=False, shift=(0, 0)):
    rgb = canvas(190, 190)
    places = [(10 + 20 * x, 10 + 20 * y) for y in range(8) for x in range(8)] if repeated else [
        (12, 14), (48, 16), (92, 30), (28, 75), (100, 98)]
    for i, (x, y) in enumerate(places):
        paint_glyph(rgb, x + shift[0], y + shift[1], 0 if repeated else i)
    return rgb


def valid_nodes():
    return ax.prune([{"nodeId": "1", "role": {"value": "button"},
                      "name": {"value": "Delete"}, "backendDOMNodeId": 17, "properties": []}])


def valid_state():
    return schema.build_state(valid_nodes(), kind="cdp", url=None, title=None,
                              ax_sha256="a" * 64, ax_stable_sha256="b" * 64,
                              prune={"name": "prune_ax_v1", "version": 1}, raw_count=1,
                              actionable_roles=ax.ACTIONABLE_ROLES)


def production_anchors(nodes):
    """Copy fixture values into the independent production Anchor type."""
    return tuple(production.Anchor(n.kind, n.x, n.y, n.d48) for n in nodes)


class RecordingCdp:
    def __init__(self):
        self.calls = []

    def send(self, method, params=None, session_id=None):
        self.calls.append(method)
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "ram-only-object"}}
        return {}


class TestPixels(unittest.TestCase):
    def test_luma_literal_vectors_and_sobel_signed_step(self):
        rgb = np.array([[(0, 0, 0), (255, 255, 255), (255, 0, 0),
                         (0, 255, 0), (0, 0, 255)]], dtype=np.uint8)
        self.assertEqual(luma(rgb).tolist(), [[0, 255, 77, 149, 29]])
        step = np.zeros((3, 6), dtype=np.int32)
        step[:, 3:] = 255
        gx, gy, mag = sobel(step)
        self.assertEqual(gx[1].tolist(), [0, 0, 1020, 1020, 0, 0])
        self.assertEqual(gy.tolist(), [[0] * 6] * 3)
        self.assertEqual(sobel(255 - step)[0][1].tolist(), [0, 0, -1020, -1020, 0, 0])
        self.assertEqual(mag[1].tolist(), [0, 0, 1020, 1020, 0, 0])
        self.assertEqual(sobel(np.full((1, 1), 255, dtype=np.int32))[2].tolist(), [[0]])
        np.testing.assert_array_equal(production.luma(rgb), luma(rgb))
        for sample in (step, 255 - step, np.full((1, 1), 255, dtype=np.int32)):
            for actual, expected in zip(production.sobel(sample), sobel(sample)):
                np.testing.assert_array_equal(actual, expected)

    def test_input_validation_and_original_contour_counterexamples(self):
        for bad in (np.zeros((2, 2, 3)), np.zeros((0, 2, 3), dtype=np.uint8),
                    np.zeros((2, 2, 4), dtype=np.uint8)):
            with self.assertRaises(ValueError):
                propose(bad)
            with self.assertRaises(ValueError):
                production.propose(bad)
        rgb = canvas()
        paint_rect(rgb, (10, 10, 70, 44))
        self.assertIn([10, 10, 70, 44], [r["box_device"] for r in propose(rgb)])
        before = rgb.copy()
        self.assertEqual(production.propose(rgb), propose(rgb))
        self.assertEqual(production.reference_detect(rgb), reference_detect(rgb))
        self.assertTrue(production.reference_detect(rgb)["proposals"])
        np.testing.assert_array_equal(rgb, before)
        # Noncontiguous input follows the same pixels without mutating them.
        self.assertEqual(production.reference_detect(rgb[:, ::-1]),
                         reference_detect(rgb[:, ::-1]))
        # A U, and a missing upper side, cannot borrow a hypothetical closing.
        rgb[8:15, 8:72] = 248
        self.assertEqual(propose(rgb), [])
        self.assertEqual(production.propose(rgb), [])
        edge = np.zeros((30, 40), dtype=np.bool_)
        edge[5:25, 5] = True
        edge[24, 5:35] = True
        self.assertLess(min(contour_support(edge, (5, 5, 35, 25))), 200)
        closed_imagination = edge.copy()
        closed_imagination[5, 5:35] = True
        closed_imagination[5:25, 34] = True
        self.assertEqual(min(contour_support(closed_imagination, (5, 5, 35, 25))), 255)
        self.assertLess(min(contour_support(edge, (5, 5, 35, 25))), 200)
        self.assertEqual(production.contour_support(edge, (5, 5, 35, 25)),
                         contour_support(edge, (5, 5, 35, 25)))
        diagonal = np.eye(4, dtype=np.bool_)
        self.assertEqual(production.component_boxes(diagonal), [(0, 0, 4, 4)])

    def test_checkbox_requires_pixel_label_context(self):
        square = canvas()
        paint_rect(square, (10, 20, 26, 36))
        self.assertFalse(any(p["role_hint"] == "checkbox" for p in propose(square)))
        labelled = square.copy()
        for x in (34, 46, 58):
            paint_glyph(labelled, x, 23)
        choices = [p for p in propose(labelled) if p["probe"] == "checkbox_context"]
        self.assertEqual([p["box_device"] for p in choices], [[10, 20, 26, 36]])
        self.assertEqual(reference_detect(labelled)["proposals"][0]["decision"], ABSTAIN)
        for rgb in (square, labelled):
            self.assertEqual(production.propose(rgb), propose(rgb))
            self.assertEqual(production.reference_detect(rgb), reference_detect(rgb))

    def test_close_requires_enclosing_upper_right_context(self):
        isolated = canvas()
        paint_x(isolated, (110, 15, 121, 26))
        self.assertFalse(any(p["probe"] == "close_context" for p in propose(isolated)))
        contextual = isolated.copy()
        paint_rect(contextual, (20, 8, 132, 78))
        self.assertIn([110, 15, 121, 26], [p["box_device"] for p in propose(contextual)
                                         if p["probe"] == "close_context"])
        multiplication = canvas()
        paint_rect(multiplication, (20, 8, 132, 78))
        paint_x(multiplication, (54, 44, 65, 55))
        self.assertFalse(any(p["probe"] == "close_context" for p in propose(multiplication)))
        for rgb in (isolated, contextual, multiplication):
            self.assertEqual(production.propose(rgb), propose(rgb))
            self.assertEqual(production.reference_detect(rgb), reference_detect(rgb))

    def test_pixel_identical_enabled_disabled_controls(self):
        enabled, disabled = canvas(), canvas()
        for frame in (enabled, disabled):
            paint_rect(frame, (10, 10, 70, 40))
        self.assertEqual(enabled.tobytes(), disabled.tobytes())
        first, second = reference_detect(enabled), reference_detect(disabled)
        self.assertEqual(schema.dumps(first), schema.dumps(second))
        self.assertTrue(first["proposals"])
        self.assertTrue(all(p["decision"] == ABSTAIN and p["enabled"] is None
                            for p in first["proposals"]))
        # Opposite hidden labels penalize calibration; they never enter detection.
        fit = pool_calibration([(5, 20, 20)])
        self.assertEqual(fit[0]["pm"], 500)
        self.assertEqual(decide(5, fit, fit, "button")["decision"], ABSTAIN)
        truth_on = [{"id": "t", "box": [10, 10, 70, 40], "role": "button", "actionable": 1}]
        truth_off = [{**truth_on[0], "actionable": 0}]
        forced = [{"id": "p", "box": truth_on[0]["box"], "decision": "button"}]
        self.assertEqual(evaluate(forced, truth_on)["tp"], 1)
        self.assertEqual(evaluate(forced, truth_off)["fp"], 1)
        self.assertEqual(production.reference_detect(enabled), first)
        self.assertEqual(production.reference_detect(disabled), second)
        self.assertEqual(production.evaluate(forced, truth_on), evaluate(forced, truth_on))
        self.assertEqual(production.evaluate(forced, truth_off), evaluate(forced, truth_off))


class TestDpiDescriptor(unittest.TestCase):
    def test_endpoint_normalization_and_width_counterexample(self):
        self.assertEqual(normalize_box((1, 3, 3, 8), 1250000), (0, 2, 3, 7))
        self.assertEqual(normalize_box((-3, -3, -1, -1), 1250000), (-3, -3, 0, 0))
        self.assertEqual(normalize_box((1, 1, 1, 1), 1250000), (0, 0, 0, 0))
        self.assertEqual(normalize_box((1, 1, 1, 3), 1250000), (0, 0, 0, 3))
        self.assertEqual(raster_box((1, 1, 1, 1), 1250000), (1, 1, 1, 1))
        # Independent width flooring loses the endpoint residue. Fusion now
        # floors both endpoints, while the detector deliberately rounds outward.
        state = {"nodes": [{"id": "n", "box": [0, 0, 2, 2], "actionable": True}]}
        reg = {"x": 1, "y": 1, "w": 1, "h": 1}
        self.assertEqual(reg["w"] * PPM // 1250000, 0)  # Original arithmetic counterexample.
        self.assertEqual(fuse_regions(state, [reg], 1250000)["hits"],
                         [{"node": "n", "region": 0, "overlap": 1, "actionable": True}])
        self.assertEqual(fuse_regions(state, [{"x": 0, "y": 0, "w": 1, "h": 1}],
                                      1250000)["hits"], [])  # Legitimately collapsed interval.
        self.assertEqual(normalize_box((1, 1, 2, 2), 1250000), (0, 0, 2, 2))
        for box in ((1, 3, 3, 8), (-3, -3, -1, -1), (1, 1, 1, 1),
                    (1, 1, 1, 3), (1, 1, 2, 2)):
            self.assertEqual(production.normalize_box(box, 1250000), normalize_box(box, 1250000))
            self.assertEqual(production.raster_box(box, 1250000), raster_box(box, 1250000))
        for bad in (0, -1, True, 1250000.0):
            with self.assertRaises(ValueError):
                normalize_box((0, 0, 3, 3), bad)

    def test_rerasterized_boxes_three_dpis_one_css_edge_tolerance(self):
        targets = [(7, 9, 46, 32), (65, 15, 84, 34), (97, 41, 142, 66)]
        for dpr in (1000000, 1250000, 2000000):
            with self.subTest(dpr_ppm=dpr):
                rgb = canvas((160 * dpr + PPM - 1) // PPM, (90 * dpr + PPM - 1) // PPM)
                for target in targets:
                    paint_rect(rgb, raster_box(target, dpr), max(1, (2 * dpr + PPM - 1) // PPM))
                found = [p["box_css"] for p in propose(rgb, dpr)]
                self.assertEqual(len(found), 3)
                for expected in targets:
                    close = [b for b in found if all(abs(a - c) <= 1 for a, c in zip(b, expected))]
                    self.assertEqual(len(close), 1, (dpr, expected, found))
                schema.dumps(reference_detect(rgb, dpr))
                self.assertEqual(production.propose(rgb, dpr), propose(rgb, dpr))
                self.assertEqual(production.reference_detect(rgb, dpr), reference_detect(rgb, dpr))
        clipped = canvas(40, 40)
        paint_rect(clipped, (0, 0, 20, 20))
        self.assertEqual(propose(clipped), [])
        self.assertEqual(production.propose(clipped), [])

    def test_d48_literal_layout_ranges_and_quantizer_boundaries(self):
        black = np.zeros((16, 16, 3), dtype=np.uint8)
        white = np.full((16, 16, 3), 255, dtype=np.uint8)
        self.assertEqual(descriptor48(black, (0, 0, 16, 16)),
                         (0,) * 44 + (0, 0, 255, 16))
        self.assertEqual(descriptor48(white, (0, 0, 16, 16)),
                         (15,) * 16 + (0,) * 28 + (255, 0, 0, 16))
        d = (15,) * 32 + (0, 15, 16, 31, 32, 127, 128, 255) * 2
        self.assertEqual(quantize48(d), (15,) * 32 + (0, 0, 1, 1, 2, 7, 8, 15) * 2)
        for rgb in (black, white):
            self.assertEqual(production.descriptor48(rgb, (0, 0, 16, 16)),
                             descriptor48(rgb, (0, 0, 16, 16)))
        self.assertEqual(production.quantize48(d), quantize48(d))
        for bad in ((0,) * 47, (16,) + (0,) * 47, (0,) * 47 + (256,), (True,) + (0,) * 47):
            with self.assertRaises(ValueError):
                quantize48(bad)
            with self.assertRaises(ValueError):
                production.quantize48(bad)
        rgb = glyph_scene()
        for anchor in extract_anchors(rgb):
            self.assertEqual(len(bytes(anchor.d48)), 48)
            self.assertTrue(all(0 <= v <= 15 for v in quantize48(anchor.d48)))

    def test_cross_dpi_box_agreement_is_not_hash_or_match_invariance(self):
        # A5 reference limitation: normalized endpoints do NOT prove descriptor
        # identity across rasterization. Do not silently advertise DPI reuse.
        target = (7, 9, 46, 32)
        nodes_by_dpr = {}
        for dpr in (1000000, 1250000, 2000000):
            rgb = canvas((160 * dpr + PPM - 1) // PPM, (90 * dpr + PPM - 1) // PPM)
            paint_rect(rgb, raster_box(target, dpr), (2 * dpr + PPM - 1) // PPM)
            candidates = propose(rgb, dpr)
            self.assertEqual(len(candidates), 1)
            found = candidates[0]
            self.assertTrue(all(abs(a - b) <= 1 for a, b in zip(found["box_css"], target)))
            nodes_by_dpr[dpr] = box_constellation(rgb, found["box_device"], dpr)
            self.assertEqual(production.box_constellation(rgb, found["box_device"], dpr),
                             production_anchors(nodes_by_dpr[dpr]))
        template = {"box": nodes_by_dpr[PPM]}
        self.assertEqual(match_templates(template, nodes_by_dpr[PPM])["best"]["votes"], 5)
        for dpr in (1250000, 2000000):
            self.assertNotEqual(fp_struct(nodes_by_dpr[PPM]), fp_struct(nodes_by_dpr[dpr]))
            self.assertIsNone(match_templates(template, nodes_by_dpr[dpr])["best"])
            self.assertIsNone(production.match_templates(
                {"box": production_anchors(nodes_by_dpr[PPM])},
                production_anchors(nodes_by_dpr[dpr]))["best"])

    def test_five_landmarks_from_pixels_and_all_permutations(self):
        rgb = canvas()
        paint_rect(rgb, (20, 20, 90, 62))
        nodes = box_constellation(rgb, (20, 20, 90, 62))
        self.assertEqual([(n.x, n.y, n.kind) for n in nodes],
                         [(20, 20, 1), (90, 20, 1), (55, 41, 2), (20, 62, 1), (90, 62, 1)])
        payload = constellation_bytes(nodes)
        self.assertEqual(len(payload), 291)
        for permutation in itertools.permutations(nodes):
            self.assertEqual(constellation_bytes(permutation), payload)
            self.assertEqual(production.constellation_bytes(production_anchors(permutation)), payload)
        self.assertEqual(production.fp_struct(production_anchors(nodes)), fp_struct(nodes))
        translated = [Anchor(n.kind, n.x + 13, n.y - 7, n.d48) for n in nodes]
        self.assertEqual(constellation_bytes(translated), payload)
        with self.assertRaises(ValueError):
            constellation_bytes(nodes[:4])
        with self.assertRaises(ValueError):
            constellation_bytes([nodes[0]] + list(nodes[:4]))


class TestHashVoting(unittest.TestCase):
    def test_hash64_and_pair_literal_byte_vectors(self):
        self.assertEqual(hash64(b""), "e3b0c44298fc1c14")
        self.assertEqual(hash64(b"abc"), "ba7816bf8f01cfea")
        self.assertEqual(production.hash64(b""), "e3b0c44298fc1c14")
        self.assertEqual(production.hash64(b"abc"), "ba7816bf8f01cfea")
        zero = (0,) * 48
        a, b = Anchor(1, 4, 10, zero), Anchor(2, -2, 20, zero)
        expected = b"WAP2\x01\x01\x02" + b"\0" * 96 + b"\xfd\xff\xff\xff\x05\x00\x00\x00"
        self.assertEqual(pair_bytes(a, b), expected)
        self.assertEqual(production.pair_bytes(*production_anchors((a, b))), expected)
        self.assertEqual(len(expected), 111)
        nodes = [Anchor(1, x, y, zero) for x, y in [(0, 0), (4, 0), (2, 2), (0, 4), (4, 4)]]
        records = [b"\x01" + bytes.fromhex(offset) + b"\0" * 48 for offset in (
            "0000000000000000", "0400000000000000", "0200000002000000",
            "0000000004000000", "0400000004000000")]
        self.assertEqual(constellation_bytes(nodes), b"WAF5\x01\x05" + b"".join(records))
        self.assertEqual(production.constellation_bytes(production_anchors(nodes)),
                         b"WAF5\x01\x05" + b"".join(records))

    def test_pair_changes_every_input_negative_rounding_and_reversal(self):
        d0, d1 = (0,) * 48, (1,) + (0,) * 47
        a, b = Anchor(1, 0, 0, d0), Anchor(2, 10, 10, d0)
        base = pair_bytes(a, b)
        alternatives = [(Anchor(3, 0, 0, d0), b), (a, Anchor(3, 10, 10, d0)),
                        (Anchor(1, 0, 0, d1), b), (a, Anchor(2, 10, 10, d1)),
                        (a, Anchor(2, 12, 10, d0)), (a, Anchor(2, 10, 12, d0)), (b, a)]
        for first, second in alternatives:
            self.assertNotEqual(pair_bytes(first, second), base)
            self.assertEqual(production.pair_bytes(*production_anchors((first, second))),
                             pair_bytes(first, second))
        self.assertEqual([signed_round(v, 2) for v in (-3, -2, -1, 0, 1, 2, 3)],
                         [-2, -1, -1, 0, 1, 1, 2])
        self.assertEqual(pair_bytes(a, Anchor(2, 9, 10, d0)), base)  # Deliberate quantization alias.
        self.assertEqual([production.signed_round(v, 2) for v in (-3, -2, -1, 0, 1, 2, 3)],
                         [-2, -1, -1, 0, 1, 1, 2])

    def test_eight_by_eight_pixel_grid_vs_distinct_anchors(self):
        repeated = extract_anchors(glyph_scene(repeated=True))
        self.assertEqual(len(repeated), 64)
        self.assertEqual(len({anchor_group(a) for a in repeated}), 1)
        grid_pairs = bounded_pairs(repeated)
        counts = defaultdict(int)
        for first, second, payload in grid_pairs:
            counts[first] += 1
        self.assertEqual(set(counts.values()), {8})
        repeated_match = match_templates({"grid": repeated[:5]}, repeated)
        self.assertEqual(repeated_match["best"]["votes"], 1)
        self.assertTrue(all(h["votes"] == 1 for h in repeated_match["hypotheses"]))
        unique = extract_anchors(glyph_scene())
        moved = extract_anchors(glyph_scene(shift=(8, 12)))
        self.assertEqual(len(unique), 5)
        self.assertEqual(len({anchor_group(a) for a in unique}), 5)
        match = match_templates({"distinct": unique}, moved)
        self.assertEqual(match["best"], {"template": "distinct", "offset_q": [4, 6],
                                         "distinct_groups": 5, "votes": 5})
        self.assertEqual(match["tie"], 0)
        self.assertLess(repeated_match["best"]["votes"], match["best"]["votes"])
        actual_repeated = production.extract_anchors(glyph_scene(repeated=True))
        actual_unique = production.extract_anchors(glyph_scene())
        actual_moved = production.extract_anchors(glyph_scene(shift=(8, 12)))
        self.assertEqual(actual_repeated, production_anchors(repeated))
        self.assertEqual(actual_unique, production_anchors(unique))
        self.assertEqual(actual_moved, production_anchors(moved))
        self.assertEqual(production.match_templates({"grid": actual_repeated[:5]}, actual_repeated),
                         repeated_match)
        self.assertEqual(production.match_templates({"distinct": actual_unique}, actual_moved), match)
        actual_pairs = production.bounded_pairs(actual_repeated)
        self.assertEqual([payload for _, _, payload in actual_pairs],
                         [payload for _, _, payload in grid_pairs])
        self.assertEqual(len(actual_pairs), 64 * 8)

    def test_vote_permutations_duplicates_ties_and_eight_cap(self):
        nodes = extract_anchors(glyph_scene())
        expected = match_templates({"z": nodes, "a": nodes}, nodes)
        self.assertEqual(expected["best"]["template"], "a")
        self.assertEqual(expected["tie"], 1)
        for permutation in itertools.permutations(nodes):
            actual = match_templates({"a": permutation, "z": reversed(nodes)}, list(reversed(permutation)) + list(permutation))
            self.assertEqual(actual, expected)
            self.assertEqual(production.match_templates(
                {"a": production_anchors(permutation), "z": production_anchors(reversed(nodes))},
                production_anchors(list(reversed(permutation)) + list(permutation))), expected)
        many = tuple(Anchor(5, i * 7, i * i, (i,) + (0,) * 47) for i in range(10))
        capped = match_templates({"ten": many}, many)["best"]
        self.assertEqual(capped["distinct_groups"], 10)
        self.assertEqual(capped["votes"], 8)
        # Multiple partners of one anchor do not add independent evidence.
        equal = tuple(Anchor(5, i * 7, i * i, (0,) * 48) for i in range(10))
        self.assertEqual(match_templates({"same": equal}, equal)["best"]["votes"], 1)
        self.assertEqual(production.match_templates({"ten": production_anchors(many)},
                                                  production_anchors(many))["best"], capped)
        self.assertEqual(production.match_templates({"same": production_anchors(equal)},
                                                  production_anchors(equal))["best"]["votes"], 1)

    def test_hash_collision_is_not_a_payload_match(self):
        from unittest.mock import patch
        a = tuple(Anchor(5, i * 5, i * 9, (i,) + (0,) * 47) for i in range(5))
        b = tuple(Anchor(5, i * 5, i * 9, (i + 6,) + (0,) * 47) for i in range(5))
        with patch(__name__ + ".hash64", return_value="0" * 16):
            self.assertIsNone(match_templates({"template": a}, b)["best"])
            expected_positive = match_templates({"template": a}, a)
            self.assertEqual(expected_positive["best"]["votes"], 5)
        with patch.object(production, "hash64", return_value="0" * 16):
            self.assertIsNone(production.match_templates(
                {"template": production_anchors(a)}, production_anchors(b))["best"])
            self.assertEqual(production.match_templates(
                {"template": production_anchors(a)}, production_anchors(a)), expected_positive)


class TestCalibrationMetrics(unittest.TestCase):
    def test_smoothed_literal_scores_count_pooling_and_cascade(self):
        self.assertEqual([smoothed(tp, fp) for tp, fp in [(0, 0), (2, 0), (1, 0), (20, 0), (0, 20)]],
                         [500, 750, 666, 954, 45])
        pooled = pool_calibration([(1, 2, 0), (2, 1, 0)])
        self.assertEqual(pooled, [{"lo": 1, "hi": 2, "scores": [1, 2], "tp": 3, "fp": 0, "pm": 800, "n": 3}])
        cascade = pool_calibration([(1, 8, 0), (2, 1, 1), (3, 0, 8)])
        self.assertEqual(cascade, [{"lo": 1, "hi": 3, "scores": [1, 2, 3], "tp": 9, "fp": 9, "pm": 500, "n": 18}])
        # Merging smoothed rates may raise a block above BOTH inputs: revisit right.
        right = pool_calibration([(1, 2, 0), (2, 1, 0), (3, 10, 2)])
        self.assertEqual(right, [{"lo": 1, "hi": 3, "scores": [1, 2, 3], "tp": 13, "fp": 2, "pm": 823, "n": 15}])
        for permutation in itertools.permutations([(1, 8, 0), (2, 1, 1), (3, 0, 8)]):
            self.assertEqual(pool_calibration(permutation), cascade)
        self.assertEqual(pool_calibration([(1, 0, 0)]), [])
        self.assertIsNone(calibration_at(pooled, 0))
        self.assertEqual(pool_calibration([(2, 1, 0), (2, 2, 0)]),
                         [{"lo": 2, "hi": 2, "scores": [2], "tp": 3, "fp": 0, "pm": 800, "n": 3}])
        gap = pool_calibration([(3, 50, 0), (4, 0, 0), (5, 20, 0)])
        self.assertEqual(gap[0]["scores"], [3, 5])
        self.assertIsNone(calibration_at(gap, 4))
        self.assertEqual(decide(4, gap, gap, "button")["decision"], ABSTAIN)
        for bins in ([(1, 2, 0), (2, 1, 0)], [(1, 8, 0), (2, 1, 1), (3, 0, 8)],
                     [(1, 2, 0), (2, 1, 0), (3, 10, 2)], [(1, 0, 0)],
                     [(3, 50, 0), (4, 0, 0), (5, 20, 0)]):
            self.assertEqual(production.pool_calibration(bins), pool_calibration(bins))
        self.assertIsNone(production.calibration_at(gap, 4))
        self.assertEqual(production.decide(4, gap, gap, "button"), decide(4, gap, gap, "button"))

    def test_calibration_units_and_split_leakage(self):
        row = {"family": "glyph-family", "unit": "one-independent-control", "split": "calibration",
               "score": 5, "correct": 1}
        self.assertEqual(calibration_examples([row] * 64), [(5, 1, 0)])
        self.assertEqual(production.calibration_examples([row] * 64), [(5, 1, 0)])
        with self.assertRaises(ValueError):
            calibration_examples([row, {**row, "correct": 0}])
        with self.assertRaises(ValueError):
            calibration_examples([{**row, "split": "test"}])
        for rows in ([row, {**row, "correct": 0}], [{**row, "split": "test"}]):
            with self.assertRaises(ValueError):
                production.calibration_examples(rows)

    def test_abstain_unknown_role_policy_boundaries(self):
        good = pool_calibration([(5, 20, 0)])
        low = pool_calibration([(5, 19, 1)])
        self.assertEqual(decide(5, [], [], "button")["decision"], ABSTAIN)
        self.assertEqual(decide(5, low, good, "button")["decision"], ABSTAIN)
        self.assertEqual(decide(5, good, low, "button")["decision"], "button")  # 909 >= 900
        self.assertEqual(decide(5, good, [], "button")["decision"], UNKNOWN)
        self.assertEqual(decide(5, good, good, UNKNOWN)["decision"], UNKNOWN)
        self.assertEqual(decide(5, good, good, "button")["decision"], "button")
        self.assertEqual(decide(5, good, good, "button", tie=1)["decision"], ABSTAIN)
        sparse = pool_calibration([(5, 19, 0)])
        self.assertEqual(decide(5, sparse, good, "button")["decision"], ABSTAIN)
        repeat_fit = pool_calibration([(1, 1000, 0)])
        self.assertEqual(decide(1, repeat_fit, repeat_fit, "button")["decision"], ABSTAIN)
        for votes, action, role, hint, tie in itertools.product(
                (1, 3, 5, 8), ([], good, low, sparse), ([], good, low),
                (UNKNOWN, "button", "checkbox"), (0, 1)):
            self.assertEqual(production.decide(votes, action, role, hint, tie),
                             decide(votes, action, role, hint, tie))

    def test_half_open_iou_exact_boundary_and_large_integers(self):
        self.assertEqual(overlap((0, 0, 3, 2), (1, 0, 4, 2)), (4, 8))
        self.assertTrue(iou_half((0, 0, 3, 2), (1, 0, 4, 2)))
        self.assertFalse(iou_half((0, 0, 3, 2), (2, 0, 5, 2)))
        self.assertEqual(overlap((0, 0, 3, 2), (3, 0, 6, 2)), (0, 12))
        self.assertFalse(iou_half((0, 0, 0, 0), (0, 0, 0, 0)))
        huge = (0, 0, 2000000000, 2000000000)
        self.assertEqual(overlap(huge, huge), (4000000000000000000, 4000000000000000000))
        with self.assertRaises(ValueError):
            overlap((4, 0, 2, 2), (0, 0, 1, 1))
        for a, b in (((0, 0, 3, 2), (1, 0, 4, 2)), ((0, 0, 3, 2), (3, 0, 6, 2)),
                     ((0, 0, 0, 0), (0, 0, 0, 0)), (huge, huge)):
            self.assertEqual(production.overlap(a, b), overlap(a, b))
            self.assertEqual(production.iou_half(a, b), iou_half(a, b))

    def test_one_to_one_permutations_duplicates_and_greedy_counterexample(self):
        pred = [{"id": "p1", "box": (1, 0, 5, 2)}, {"id": "p2", "box": (-1, 0, 3, 2)}]
        truth = [{"id": "g1", "box": (0, 0, 4, 2)}, {"id": "g2", "box": (2, 0, 6, 2)}]
        expected = [{"prediction": "p1", "truth": "g1", "inter": 6, "union": 10}]
        for ps, ts in itertools.product(itertools.permutations(pred), itertools.permutations(truth)):
            self.assertEqual(one_to_one(ps, ts), expected)
            self.assertEqual(production.one_to_one(ps, ts), expected)
        self.assertTrue(iou_half(pred[0]["box"], truth[1]["box"]))
        self.assertTrue(iou_half(pred[1]["box"], truth[0]["box"]))  # A maximum assignment could take two.
        with self.assertRaises(ValueError):
            one_to_one(pred + [pred[0]], truth)
        with self.assertRaises(ValueError):
            production.one_to_one(pred + [pred[0]], truth)
        dupe_boxes = [{"id": "p1", "box": (0, 0, 4, 2)}, {"id": "p2", "box": (0, 0, 4, 2)}]
        self.assertEqual(len(one_to_one(dupe_boxes, truth[:1])), 1)

    def test_recall_precision_unknown_abstain_and_disabled_truth(self):
        truth = [{"id": "t1", "box": (0, 0, 10, 10), "role": "button", "actionable": 1},
                 {"id": "t2", "box": (20, 0, 30, 10), "role": "checkbox", "actionable": 1},
                 {"id": "t3", "box": (40, 0, 50, 10), "role": "button", "actionable": 0}]
        pred = [{"id": "p1", "box": truth[0]["box"], "decision": "checkbox"},
                {"id": "p2", "box": truth[1]["box"], "decision": UNKNOWN},
                {"id": "p3", "box": truth[2]["box"], "decision": "button"},
                {"id": "p4", "box": (60, 0, 70, 10), "decision": ABSTAIN}]
        metrics = evaluate(pred, truth)
        self.assertEqual((metrics["class_agnostic_recall_pm"], metrics["role_conditioned_precision_pm"]), (1000, 0))
        self.assertEqual((metrics["tp"], metrics["fp"], metrics["role_fp"]), (2, 1, 2))
        self.assertEqual(metrics["selective_risk_pm"], 333)
        self.assertEqual(metrics["selection_coverage_pm"], 750)
        self.assertEqual(metrics["role_coverage_pm"], 666)
        abstained = evaluate([{**pred[0], "decision": ABSTAIN}], truth[:1])
        self.assertEqual((abstained["proposal_recall_pm"], abstained["class_agnostic_recall_pm"]), (1000, 0))
        self.assertEqual(production.evaluate(pred, truth), metrics)
        self.assertEqual(production.evaluate([{**pred[0], "decision": ABSTAIN}], truth[:1]), abstained)

    def test_zero_denominators_are_unavailable(self):
        empty = evaluate([], [])
        for name in ("proposal_recall_pm", "class_agnostic_recall_pm", "role_conditioned_precision_pm",
                     "action_precision_pm", "selection_coverage_pm", "role_coverage_pm", "selective_risk_pm"):
            self.assertIsNone(empty[name])
        t = [{"id": "t", "box": (0, 0, 10, 10), "role": "button", "actionable": 1}]
        self.assertEqual(evaluate([], t)["class_agnostic_recall_pm"], 0)
        p = [{"id": "p", "box": (0, 0, 10, 10), "decision": UNKNOWN}]
        missing = evaluate(p, [])
        self.assertEqual((missing["action_precision_pm"], missing["selective_risk_pm"]), (0, 1000))
        self.assertIsNone(missing["class_agnostic_recall_pm"])
        self.assertIsNone(missing["role_conditioned_precision_pm"])
        for predictions, truth in (([], []), ([], t), (p, [])):
            self.assertEqual(production.evaluate(predictions, truth), evaluate(predictions, truth))


class TestReferenceSafety(unittest.TestCase):
    def test_zero_false_irreversible_admissions_with_positive_control(self):
        fit = pool_calibration([(8, 1000, 0)])
        vision = decide(8, fit, fit, "button")
        self.assertEqual(vision["decision"], "button")
        self.assertNotIn(vision["level"], ("L0_EXACT", "L1_STABLE"))
        trusted = {"provenance": "cdp", "corroborated": 1, "level": "L0_EXACT", "fp_exact": "a" * 16,
                   "fresh": 1, "tie": 0, "enabled": 1, "authorized": 1}
        self.assertTrue(irreversible_allowed(trusted))
        for invalid_flag in (True, 1.0, "1"):
            self.assertFalse(irreversible_allowed({**trusted, "corroborated": invalid_flag}))
        attacks = [vision, {**trusted, "provenance": "vision"},
                   {**trusted, "provenance": "vision", "level": "L1_STABLE", "fp_stable": "b" * 16},
                   {**trusted, "fp_exact": None}, {**trusted, "corroborated": 0},
                   {**trusted, "enabled": 0}, {**trusted, "fresh": 0},
                   {**trusted, "tie": 1}, {**trusted, "authorized": 0}]
        for decision in (ABSTAIN, UNKNOWN, "checkbox", "button"):
            attacks.append({**vision, "decision": decision, "votes": 8, "actionability_pm": 1000})
        self.assertEqual(sum(int(irreversible_allowed(x)) for x in attacks), 0)
        self.assertTrue(production.irreversible_allowed(trusted))
        self.assertEqual(sum(int(production.irreversible_allowed(x)) for x in attacks), 0)
        schema.dumps({"false_irreversible_admissions": 0, "cases": len(attacks), "records": attacks})
        # Positive integrated pixel -> descriptor -> template -> calibration path.
        enabled, disabled = canvas(), canvas()
        for frame in (enabled, disabled):
            paint_rect(frame, (10, 20, 26, 36))
            for x in (34, 46, 58):
                paint_glyph(frame, x, 23)
        proposals = propose(enabled)
        templates = {"shape%02d" % i: box_constellation(enabled, p["box_device"])
                     for i, p in enumerate(proposals)}
        # Fabricated calibration counts exercise the contract, not empirical accuracy.
        strong = pool_calibration([(score, 1000, 0) for score in range(3, 9)])
        first = reference_detect(enabled, templates=templates, action_fit=strong, role_fit=strong)
        second = reference_detect(disabled, templates=templates, action_fit=strong, role_fit=strong)
        self.assertEqual(schema.dumps(first), schema.dumps(second))
        chosen = [p for p in first["proposals"] if p["decision"] == "checkbox"]
        self.assertEqual(len(chosen), 1)
        self.assertEqual((chosen[0]["votes"], chosen[0]["match"]["tie"]), (5, 0))
        self.assertIsNone(chosen[0]["enabled"])
        all_outputs = first["proposals"] + second["proposals"]
        self.assertEqual(sum(int(irreversible_allowed(p)) for p in all_outputs), 0)
        self.assertTrue(all(p["level"] not in ("L0_EXACT", "L1_STABLE") for p in all_outputs))
        production_templates = {name: production_anchors(nodes) for name, nodes in templates.items()}
        actual = production.reference_detect(enabled, templates=production_templates,
                                             action_fit=strong, role_fit=strong)
        self.assertEqual(actual, first)
        self.assertEqual(production.reference_detect(disabled, templates=production_templates,
                                                    action_fit=strong, role_fit=strong), second)
        self.assertEqual(production.pool_calibration([(score, 1000, 0) for score in range(3, 9)]), strong)
        viewport = {"w": 160, "h": 100, "dpr_ppm": PPM}
        prune = {"name": "perceive_v_v1", "version": 1}
        state = production.to_state(actual, url="https://example.test", title="RAM checkbox",
                                    viewport=viewport, prune=prune)
        self.assertEqual(schema.validate_state(state), [])
        self.assertEqual(state["source"]["kind"], "vision")
        self.assertEqual(state["actionable"], [])
        self.assertTrue(any(n["role"] == "checkbox" for n in state["nodes"]))
        self.assertTrue(all(n["actionable"] is False and n["states"] == {} for n in state["nodes"]))
        self.assertEqual(sum(int(production.irreversible_allowed(p)) for p in actual["proposals"]), 0)
        self.assertTrue(all(p["provenance"] == "vision" and p["enabled"] is None
                            and p["corroborated"] == p["irreversible_admitted"] == 0
                            and p["level"] not in ("L0_EXACT", "L1_STABLE")
                            for p in actual["proposals"]))

    def test_serialization_full_outputs_no_floats_and_no_vision_state_kind(self):
        rgb = canvas()
        paint_rect(rgb, (10, 10, 70, 40))
        output = reference_detect(rgb)
        combined = {"detection": output, "calibration": pool_calibration([(5, 30, 1)]),
                    "evaluation": evaluate(output["proposals"], [])}
        encoded = schema.dumps(combined)
        def forbidden(value):
            raise AssertionError("float decoded: " + value)
        self.assertEqual(json.loads(encoded, parse_float=forbidden), combined)
        with self.assertRaises(schema.StateError):
            schema.dumps({"nested": [combined, {"bad": 0.5}]})
        state = valid_state()
        self.assertEqual(schema.validate_state(state), [])
        state["source"]["kind"] = "vision"
        self.assertEqual(schema.validate_state(state), [])
        state["source"]["kind"] = "unknown-kind"
        self.assertTrue(any("source.kind" in error for error in schema.validate_state(state)))

        # Keep the historical method name/claim mapping, but now verify the
        # separate production projection accepts vision and preserves safety.
        import copy
        actual = production.reference_detect(rgb)
        self.assertEqual(actual, output)
        self.assertTrue(actual["proposals"])
        viewport = {"w": 160, "h": 100, "dpr_ppm": PPM}
        prune = {"name": "perceive_v_v1", "version": 1}
        metadata = {"url": "https://example.test", "title": "RAM rectangles",
                    "viewport": viewport, "prune": prune}
        original = copy.deepcopy((actual, metadata))
        projected = production.to_state(actual, **metadata)
        self.assertEqual(schema.validate_state(projected), [])
        self.assertEqual(projected["schema"], "wender.state/v1")
        self.assertEqual(projected["source"]["kind"], "vision")
        self.assertEqual(projected["actionable"], [])
        self.assertEqual(len(projected["nodes"]), len(actual["proposals"]))
        for node in projected["nodes"]:
            self.assertEqual(set(node), set(schema.NODE_KEYS))
            self.assertFalse(node["actionable"])
            self.assertIsNone(node["parent"])
            self.assertEqual(node["states"], {})
            self.assertEqual(node["box"], [10, 10, 60, 30])
        self.assertEqual(json.loads(schema.dumps(projected), parse_float=forbidden), projected)
        self.assertEqual((actual, metadata), original)
        self.assertIsNot(projected["viewport"], viewport)
        self.assertIsNot(projected["prune"], prune)

        # Stable identity comes from canonical vision geometry/structure; the
        # required legacy digest slots carry vision serialization, never AX proof.
        proposal = actual["proposals"][0]
        identity = {k: proposal[k] for k in ("box", "fp_struct", "probe", "role_hint")}
        canonical = schema.dumps(identity).encode("utf-8")
        self.assertEqual(projected["nodes"][0]["id"],
                         hashlib.sha256(b"WAVS1\x01node\x00" + canonical).hexdigest()[:16])
        self.assertEqual(projected["ax_sha256"], hashlib.sha256(
            b"WAVS1\x01exact\x00" + schema.dumps(actual).encode("utf-8")).hexdigest())
        stable = {"schema": actual["schema"], "dpr_ppm": PPM, "identities": [identity]}
        self.assertEqual(projected["ax_stable_sha256"], hashlib.sha256(
            b"WAVS1\x01stable\x00" + schema.dumps(stable).encode("utf-8")).hexdigest())
        changed = copy.deepcopy(actual)
        changed["proposals"][0]["decision"] = "button"
        selected = production.to_state(changed, **metadata)
        self.assertEqual(selected["nodes"][0]["id"], projected["nodes"][0]["id"])
        self.assertEqual(selected["ax_stable_sha256"], projected["ax_stable_sha256"])
        self.assertNotEqual(selected["ax_sha256"], projected["ax_sha256"])
        self.assertEqual(selected["nodes"][0]["role"], "button")
        self.assertFalse(selected["nodes"][0]["actionable"])
        self.assertEqual(selected["actionable"], [])

        paint_rect(rgb, (90, 55, 145, 85))
        several = production.reference_detect(rgb)
        self.assertEqual(len(several["proposals"]), 2)
        permuted = {**several, "proposals": list(reversed(several["proposals"]))}
        self.assertEqual(schema.dumps(production.to_state(several, **metadata)),
                         schema.dumps(production.to_state(permuted, **metadata)))
        empty = production.to_state(production.reference_detect(canvas()), **metadata)
        self.assertEqual(schema.validate_state(empty), [])
        self.assertEqual(empty["nodes"], [])

        for field, value in (("provenance", "cdp"), ("enabled", 1), ("corroborated", 1),
                             ("irreversible_admitted", 1), ("level", "L0_EXACT"),
                             ("level", "L1_STABLE"), ("box", [10, 10, 70.0, 40])):
            spoofed = copy.deepcopy(actual)
            spoofed["proposals"][0][field] = value
            with self.assertRaises((ValueError, schema.StateError)):
                production.to_state(spoofed, **metadata)
        floated = copy.deepcopy(actual)
        floated["extra"] = [{"nested": {0.5: "float key"}}]
        with self.assertRaises(schema.StateError):
            production.to_state(floated, **metadata)
        # Duplicate identities must not silently consume one node or alias ids.
        duplicate = copy.deepcopy(actual)
        duplicate["proposals"].append({**duplicate["proposals"][0], "id": "second"})
        with self.assertRaises(ValueError):
            production.to_state(duplicate, **metadata)

    def test_valid_live_probe_setup_and_structured_control(self):
        node = valid_nodes()[0]
        self.assertEqual(ax.describe(node)["role"], "button")
        match = ax.match([node], ax.describe(node))
        self.assertEqual(match["level"], "L0_EXACT")
        self.assertFalse(match["tie"])
        self.assertNotIn("established", match)
        cdp = RecordingCdp()
        receipt = cdp_executor.run_ax_action(cdp, "ram", [node],
                                             {"op": "click", "target": node["id"], "irreversible": True},
                                             lambda: [node], retries=1, wait_ms=0, sleep=lambda _: None)
        self.assertEqual(receipt["ok"], 1)
        self.assertIn("Runtime.callFunctionOn", cdp.calls)


class TestLiveBridgeGaps(unittest.TestCase):
    def setUp(self):
        # A5-D1: imports, frame creation and calls happen OUTSIDE expectedFailure.
        # Unexpected setup errors are ordinary ERRORs, never excused as xfails.
        self.frame = Frame(8, 8, bytes(8 * 8 * 3))
        self.observed = {}
        for name, args in (("screen_identity", (self.frame,)),
                           ("changed_regions", (self.frame, self.frame))):
            try:
                self.observed[name] = ("returned", getattr(vision_bridge, name)(*args))
            except NotImplementedError as exc:
                if str(exc) != "vision-engine bridge is a v0 stub":
                    raise
                self.observed[name] = ("documented_stub", str(exc))

    def test_a5_d1_live_screen_identity_contract(self):
        # A5-D1: validated Frame input reaches the deterministic RAM bridge.
        self.assertEqual(self.observed["screen_identity"][0], "returned")

    def test_a5_d1_live_changed_regions_contract(self):
        # A5-D1: narrow behavior; unexpected setup errors remain ordinary errors.
        self.assertEqual(self.observed["changed_regions"][0], "returned")


class TestLiveSafetyGap(unittest.TestCase):
    def setUp(self):
        # A5-D2: valid AX fixture and fake transport, all setup outside xfail.
        self.nodes = valid_nodes()
        self.cdp = RecordingCdp()
        node = self.nodes[0]
        entry = {"role": node["role"], "name": node["name"], "ordinal": node["ordinal"],
                 "fp_exact": None, "fp_stable": None, "provenance": "vision"}
        self.match = ax.match(self.nodes, entry)
        try:
            self.receipt = cdp_executor.run_ax_action(
                self.cdp, "ram", self.nodes,
                {"op": "click", "target": node["id"], "irreversible": True, "describe": entry},
                lambda: self.nodes, retries=1, wait_ms=0, sleep=lambda _: None)
        except cdp_executor.ActionError:
            self.receipt = None  # Desired refusal after a future production fix.
        self.admitted = "Runtime.callFunctionOn" in self.cdp.calls

    def test_a5_d2_missing_fingerprint_irreversible_is_refused(self):
        # Deferred integration dependency: F1 ax.py + F2 cdp_executor.py
        # hardening is outside this task's exclusive ownership. Keep refusal.
        # A5-D2: current code admits missing-fingerprint established L0. No real action.
        self.assertFalse(self.admitted, "uncorroborated vision entry reached fake irreversible dispatch")


CLAIM_TESTS = {
    "A5-R01": ("TestPixels.test_luma_literal_vectors_and_sobel_signed_step",),
    "A5-R02": ("TestPixels.test_input_validation_and_original_contour_counterexamples",),
    "A5-R03": ("TestPixels.test_checkbox_requires_pixel_label_context", "TestPixels.test_close_requires_enclosing_upper_right_context"),
    "A5-R04": ("TestPixels.test_pixel_identical_enabled_disabled_controls",),
    "A5-R05": ("TestDpiDescriptor.test_endpoint_normalization_and_width_counterexample", "TestDpiDescriptor.test_rerasterized_boxes_three_dpis_one_css_edge_tolerance", "TestDpiDescriptor.test_cross_dpi_box_agreement_is_not_hash_or_match_invariance"),
    "A5-R06": ("TestDpiDescriptor.test_d48_literal_layout_ranges_and_quantizer_boundaries",),
    "A5-R07": ("TestDpiDescriptor.test_five_landmarks_from_pixels_and_all_permutations", "TestHashVoting.test_hash64_and_pair_literal_byte_vectors"),
    "A5-R08": ("TestHashVoting.test_pair_changes_every_input_negative_rounding_and_reversal", "TestHashVoting.test_hash_collision_is_not_a_payload_match"),
    "A5-R09": ("TestHashVoting.test_eight_by_eight_pixel_grid_vs_distinct_anchors", "TestHashVoting.test_vote_permutations_duplicates_ties_and_eight_cap"),
    "A5-R10": ("TestCalibrationMetrics.test_smoothed_literal_scores_count_pooling_and_cascade", "TestCalibrationMetrics.test_calibration_units_and_split_leakage"),
    "A5-R11": ("TestCalibrationMetrics.test_abstain_unknown_role_policy_boundaries",),
    "A5-R12": ("TestCalibrationMetrics.test_half_open_iou_exact_boundary_and_large_integers", "TestCalibrationMetrics.test_one_to_one_permutations_duplicates_and_greedy_counterexample"),
    "A5-R13": ("TestCalibrationMetrics.test_recall_precision_unknown_abstain_and_disabled_truth", "TestCalibrationMetrics.test_zero_denominators_are_unavailable"),
    "A5-R14": ("TestReferenceSafety.test_zero_false_irreversible_admissions_with_positive_control",),
    "A5-R15": ("TestReferenceSafety.test_serialization_full_outputs_no_floats_and_no_vision_state_kind", "TestReferenceSafety.test_valid_live_probe_setup_and_structured_control"),
    "A5-D1": ("TestLiveBridgeGaps.test_a5_d1_live_screen_identity_contract", "TestLiveBridgeGaps.test_a5_d1_live_changed_regions_contract"),
    "A5-D2": ("TestLiveSafetyGap.test_a5_d2_missing_fingerprint_irreversible_is_refused",),
    "A5-R16": ("TestDocumentation.test_claim_mapping_from_utf8_sibling_module",),
}


class TestDocumentation(unittest.TestCase):
    def test_claim_mapping_from_utf8_sibling_module(self):
        root = Path(__file__).resolve().parents[1]
        # Read the sibling reference module AS UTF-8. No generated artifacts.
        sibling = root / "tests" / "test_jq1_a5_affordance_constellation_spec.py"
        source = sibling.read_text(encoding="utf-8")
        doc = (root / "docs" / "spec" / "jq1_a5_affordance_constellation_spec.md").read_text(encoding="utf-8")
        module = ast.parse(source)
        actual = {node.name + "." + member.name for node in module.body if isinstance(node, ast.ClassDef)
                  for member in node.body if isinstance(member, ast.FunctionDef) and member.name.startswith("test_")}
        mapped = {test for tests in CLAIM_TESTS.values() for test in tests}
        self.assertEqual(actual, mapped)
        for claim, tests in CLAIM_TESTS.items():
            rows = [line for line in doc.splitlines() if line.startswith("| " + claim + " |")]
            self.assertEqual(len(rows), 1, claim)
            for name in tests:
                self.assertIn(name, rows[0])
            for consumer in ("vision_adapter", "vision_bridge", "affordance_adversarial"):
                self.assertIn(consumer, rows[0])
        for absent in ("production detector", "d48 layout", "hash encoding", "calibration API", "vision state kind"):
            self.assertTrue(any(absent in line and "AUSENTE" in line for line in doc.splitlines()), absent)
        # API/default compatibility is required; the independent helper bodies
        # above remain the oracle for the dynamic production comparisons.
        import inspect
        fixture_start = next(node.lineno for node in module.body
                             if isinstance(node, ast.FunctionDef) and node.name == "canvas")
        helpers = [node.name for node in module.body
                   if isinstance(node, ast.FunctionDef) and node.lineno < fixture_start]
        for name in helpers:
            self.assertTrue(callable(getattr(production, name)), name)
            self.assertEqual(inspect.signature(getattr(production, name)),
                             inspect.signature(globals()[name]), name)
        self.assertEqual(list(inspect.signature(production.to_state).parameters),
                         ["envelope", "url", "title", "viewport", "prune"])
        for name in ("url", "title", "viewport", "prune"):
            self.assertEqual(inspect.signature(production.to_state).parameters[name].kind,
                             inspect.Parameter.KEYWORD_ONLY)
        production_source = Path(production.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(production_source)):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("tests"))
            elif isinstance(node, ast.Import):
                self.assertFalse(any(alias.name.startswith("tests") for alias in node.names))


if __name__ == "__main__":
    unittest.main()
