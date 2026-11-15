"""Oracle label record scoring against vision engine tile output.

Maps vqa.oracle.label/v1 records onto engine tile grid (s, gw, gh) obtained
from visioncore.layout.grid_params(W, H).
"""
import numpy as np

from visioncore import layout

NAMES = list(layout.CLS)
CLS_ID = dict(layout.CLS_ID)


class ScoreError(ValueError):
    rc = 5

    def __init__(self, code, detail=""):
        self.code = code
        super().__init__(code + (": " + str(detail) if detail else ""))


def _class(value):
    if isinstance(value, str) and value in CLS_ID:
        return CLS_ID[value]
    if isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_)) and 0 <= value < 7:
        return int(value)
    raise ScoreError("E_SCORE_LABEL", "invalid class: %r" % (value,))


def _integer(value, code="E_SCORE_LABEL"):
    if not isinstance(value, (int, np.integer)) or isinstance(value, (bool, np.bool_)):
        raise ScoreError(code, "integer required")
    return int(value)


def score_oracle(records, cls, W, H, mv_dx=None, mv_dy=None):
    """Score engine classification matrix against oracle label records.

    Parameters
    ----------
    records : list of dict
        Label records shaped like vqa.oracle.label/v1.
    cls : np.ndarray
        Engine predicted class array of shape (frames, gh, gw).
    W : int
        Image width in pixels.
    H : int
        Image height in pixels.
    mv_dx : np.ndarray, optional
        Engine predicted horizontal motion vectors of shape (frames, gh, gw).
    mv_dy : np.ndarray, optional
        Engine predicted vertical motion vectors of shape (frames, gh, gw).

    Returns
    -------
    dict
        Output dict containing confusion matrix (7x7), support per class,
        straddle_count, mv_exact, mv_total, untested list, and diagnostics.
    """
    s, gw, gh = layout.grid_params(W, H)
    cls = np.asarray(cls)
    if cls.ndim != 3 or cls.shape[1:] != (gh, gw) or cls.dtype.kind not in "iu":
        raise ScoreError("E_SCORE_ARRAY", "cls must be integer array of shape (frames, %d, %d)" % (gh, gw))
    if cls.size > 0 and (cls.min() < 0 or cls.max() >= 7):
        raise ScoreError("E_SCORE_ARRAY", "predicted class outside 0..6")

    frames = cls.shape[0]

    if mv_dx is None:
        mv_dx = np.zeros_like(cls, dtype=np.int64)
    else:
        mv_dx = np.asarray(mv_dx)
        if mv_dx.shape != cls.shape or mv_dx.dtype.kind not in "iu":
            raise ScoreError("E_SCORE_ARRAY", "mv_dx shape must match cls shape")

    if mv_dy is None:
        mv_dy = np.zeros_like(cls, dtype=np.int64)
    else:
        mv_dy = np.asarray(mv_dy)
        if mv_dy.shape != cls.shape or mv_dy.dtype.kind not in "iu":
            raise ScoreError("E_SCORE_ARRAY", "mv_dy shape must match cls shape")

    exact_records = []
    derived_records = []

    for rec in records:
        conf = rec.get("confidence", "exact")
        if conf == "derived":
            derived_records.append(rec)
        else:
            exact_records.append(rec)

    # Frame-by-frame tile coverage
    # tile_records[f][r][c] = list of (ideal_class, (mv_dx, mv_dy))
    tile_coverage = [[[[] for _ in range(gw)] for _ in range(gh)] for _ in range(frames)]

    for rec in exact_records:
        f0 = _integer(rec["f0"])
        f1 = _integer(rec["f1"])
        ideal_cls = _class(rec["ideal"])
        mv = rec.get("mv")
        if ideal_cls == 3:  # MOVED
            if not isinstance(mv, (list, tuple)) or len(mv) != 2:
                raise ScoreError("E_SCORE_LABEL", "MOVED requires exact mv [dx, dy]")
            mv_vec = (_integer(mv[0]), _integer(mv[1]))
        else:
            mv_vec = (0, 0) if mv is None else (_integer(mv[0]), _integer(mv[1]))

        rects = rec.get("rects", [])
        if not isinstance(rects, (list, tuple)):
            raise ScoreError("E_SCORE_LABEL", "rects must be list of [x, y, w, h]")

        # Active frame range inclusive [f0, f1]
        frame_start = max(0, f0)
        frame_end = min(frames - 1, f1)

        for rect in rects:
            if not isinstance(rect, (list, tuple)) or len(rect) != 4:
                raise ScoreError("E_SCORE_LABEL", "rect must be [x, y, w, h]")
            x, y, w, h = [_integer(v) for v in rect]
            if w < 1 or h < 1 or x < 0 or y < 0:
                raise ScoreError("E_SCORE_LABEL", "invalid rect dimensions")

            c_min = max(0, x // s)
            c_max = min(gw - 1, (x + w - 1) // s)
            r_min = max(0, y // s)
            r_max = min(gh - 1, (y + h - 1) // s)

            for f in range(frame_start, frame_end + 1):
                for r in range(r_min, r_max + 1):
                    for c in range(c_min, c_max + 1):
                        tile_coverage[f][r][c].append((ideal_cls, mv_vec))

    confusion = np.zeros((7, 7), dtype=np.int64)
    support_counts = np.zeros(7, dtype=np.int64)
    straddle_count = 0
    mv_exact = 0
    mv_total = 0

    for f in range(frames):
        for r in range(gh):
            for c in range(gw):
                cov = tile_coverage[f][r][c]
                if not cov:
                    ideal_cls = 0  # UNCHANGED
                    pred_cls = int(cls[f, r, c])
                    confusion[0, pred_cls] += 1
                    support_counts[0] += 1
                else:
                    distinct_classes = {item[0] for item in cov}
                    if len(distinct_classes) > 1:
                        straddle_count += 1
                    else:
                        ideal_cls = next(iter(distinct_classes))
                        pred_cls = int(cls[f, r, c])
                        confusion[ideal_cls, pred_cls] += 1
                        support_counts[ideal_cls] += 1

                        if ideal_cls == 3:  # MOVED
                            mv_total += 1
                            exp_dx, exp_dy = cov[0][1]
                            if pred_cls == 3 and int(mv_dx[f, r, c]) == exp_dx and int(mv_dy[f, r, c]) == exp_dy:
                                mv_exact += 1

    support_dict = {name: int(support_counts[i]) for i, name in enumerate(NAMES)}
    untested = [name for i, name in enumerate(NAMES) if support_counts[i] == 0]

    diagnostics = {
        "derived_records_count": len(derived_records),
        "exact_records_count": len(exact_records),
        "total_records_count": len(records),
        "tile_frames": int(frames * gh * gw),
        "scored_tile_frames": int(frames * gh * gw - straddle_count),
        "straddle_count": straddle_count,
    }

    per_class = {}
    for i, name in enumerate(NAMES):
        pred_sum = int(confusion[:, i].sum())
        ideal_sum = int(confusion[i, :].sum())
        corr = int(confusion[i, i])
        prec_num, prec_den = corr, pred_sum
        rec_num, rec_den = corr, ideal_sum
        per_class[name] = {
            "precision": {
                "fraction": "%d/%d" % (prec_num, prec_den) if prec_den else "0/1",
                "defined": bool(prec_den),
                "numerator": prec_num,
                "denominator": prec_den,
            },
            "recall": {
                "fraction": "%d/%d" % (rec_num, rec_den) if rec_den else "0/1",
                "defined": bool(rec_den),
                "numerator": rec_num,
                "denominator": rec_den,
            },
            "support": ideal_sum,
        }

    return {
        "confusion": confusion.tolist(),
        "support": support_dict,
        "straddle_count": straddle_count,
        "straddles": straddle_count,
        "mv_exact": mv_exact,
        "mv_total": mv_total,
        "untested": untested,
        "diagnostics": diagnostics,
        "per_class": per_class,
        "grid": {"s": s, "gw": gw, "gh": gh, "W": W, "H": H},
    }


score = score_oracle
