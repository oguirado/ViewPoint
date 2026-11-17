"""Integer mask arithmetic and explicit ordinal policy; no learned-model fallback."""
import math

PPM = 1_000_000


class RankError(Exception):
    def __init__(self, rc, reason):
        self.rc, self.reason = rc, reason
        super().__init__(reason)


def require(condition, rc, reason):
    if not condition:
        raise RankError(rc, reason)


def silhouette_iou_ppm(left, right):
    """Masks are nonempty, equally sized bytes of binary 0/255 pixels. Floor."""
    require(len(left) == len(right) and len(left) > 0, 2, "mask_size_mismatch")
    require(all(v in (0, 255) for v in left + right), 2, "mask_not_binary")
    require(any(left) and any(right), 3, "empty_silhouette")
    intersection = sum(a == 255 and b == 255 for a, b in zip(left, right))
    union = sum(a == 255 or b == 255 for a, b in zip(left, right))
    return intersection * PPM // union


def cosine_ppm(left, right):
    """CPU CLS cosine, clamped to [-1,1], shifted to [0,1], then floored."""
    require(len(left) == len(right) and len(left) > 0, 2, "embedding_size_mismatch")
    require(all(type(v) in (int, float) and math.isfinite(v) for v in (*left, *right)),
            2, "embedding_not_finite")
    a = math.sqrt(math.fsum(v * v for v in left))
    b = math.sqrt(math.fsum(v * v for v in right))
    require(math.isfinite(a) and math.isfinite(b) and a > 0 and b > 0,
            3, "embedding_zero_norm")
    similarity = math.fsum((x / a) * (y / b) for x, y in zip(left, right))
    return math.floor((min(1.0, max(-1.0, similarity)) + 1.0) * PPM / 2)


def order(rows, policy):
    """Gate eligibility first, weighted IoU/cosine next, integer seed/id ties.

    Weights and floors are caller-declared DESIGN_CHOICE, never calibrated here.
    A ranking is a preference, not an asset acceptance decision.
    """
    require(bool(rows), 3, "no_candidates")
    total = policy["iou_weight"] + policy["dino_weight"]
    result = []
    for row in rows:
        item = dict(row)
        if item["dino_ppm"] is None:
            item.update(composite_ppm=None, eligible=False)
        else:
            item["composite_ppm"] = (item["iou_ppm"] * policy["iou_weight"] +
                                     item["dino_ppm"] * policy["dino_weight"]) // total
            item["eligible"] = (item["gate_rc"] == 0 and
                                item["iou_ppm"] >= policy["min_iou_ppm"] and
                                item["dino_ppm"] >= policy["min_dino_ppm"])
        result.append(item)
    return sorted(result, key=lambda r: (not r["eligible"],
                                         -(r["composite_ppm"] or 0), -r["iou_ppm"],
                                         -(r["dino_ppm"] or 0),
                                         r["seed"] if r["seed"] is not None else -1, r["id"]))
