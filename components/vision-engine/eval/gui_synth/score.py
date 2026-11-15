"""Exact, engine-independent tile-frame scoring for the GUI synthetic bench.

Intervals are half-open [f0, f1); tiles are already Morton indices from Grid.
Confusion matrices have truth on rows and predictions on columns. Missing
labels mean UNCHANGED. Empty precision/recall denominators are explicitly
undefined (``defined: false``); their conventional serialized value is 0/1.
"""
from fractions import Fraction
import hashlib
import json
import math

import numpy as np

NAMES = ["UNCHANGED", "JITTER", "CHANGED", "MOVED", "FLICKER", "FROZEN", "DRIFT"]
_LEGACY = {"CHANGED": {2}, "MOVED": {3}, "FLICKER": {4},
           "CHANGED_REGION": {2, 3}, "NOT_CHANGED": {0, 1},
           "UNCHANGED": {0}, "JITTER": {1}, "FROZEN": {5}, "DRIFT": {6}}


class ScoreError(ValueError):
    rc = 5

    def __init__(self, code, detail=""):
        self.code = code
        super().__init__(code + (": " + str(detail) if detail else ""))


def _class(value):
    if isinstance(value, str) and value in NAMES:
        return NAMES.index(value)
    if isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_)) and 0 <= value < 7:
        return int(value)
    raise ScoreError("E_SCORE_LABEL", "invalid class")


def _integer(value, code="E_SCORE_LABEL"):
    if not isinstance(value, (int, np.integer)) or isinstance(value, (bool, np.bool_)):
        raise ScoreError(code, "integer required")
    return int(value)


def _array(value, shape=None, classes=False):
    a = np.asarray(value)
    if a.ndim != 2 or a.dtype.kind not in "iu" or (shape is not None and a.shape != shape):
        raise ScoreError("E_SCORE_ARRAY", "integer [F,N] arrays with equal shapes required")
    if classes and (not a.size or a.min() < 0 or a.max() >= 7):
        raise ScoreError("E_SCORE_ARRAY", "class outside 0..6 or empty array")
    return a


def _ratio(num, den):
    f = Fraction(int(num), int(den)) if den else Fraction(0)
    return {"fraction": str(f.numerator) + "/" + str(f.denominator),
            "permille": int(1000 * f), "defined": bool(den),
            "numerator": int(num), "denominator": int(den)}


def _metrics(confusion, mv_correct, mv_total, fp, fp_total, engine_ms, generator_ms, frames):
    c = np.asarray(confusion, dtype=np.int64)
    per_class = {}
    flat = {}
    for i, name in enumerate(NAMES):
        precision = _ratio(c[i, i], c[:, i].sum())
        recall = _ratio(c[i, i], c[i].sum())
        per_class[name] = {"precision": precision, "recall": recall,
                           "support": int(c[i].sum())}
        flat["precision_" + name] = precision["fraction"] if precision["defined"] else None
        flat["recall_" + name] = recall["fraction"] if recall["defined"] else None
    mv = _ratio(mv_correct, mv_total)
    fp_rate = Fraction(fp * 1000000, fp_total) if fp_total else None
    flat.update(mv_exact=mv["fraction"] if mv["defined"] else None,
                fp_ppm=None if fp_rate is None else str(fp_rate),
                ms_per_frame=None if not frames else engine_ms / frames,
                generator_ms_per_frame=None if not frames else generator_ms / frames)
    return {"confusion": c.tolist(), "per_class": per_class, "metrics": flat,
            "support_moved": int(mv_total), "mv_exact": mv, "mv_correct": int(mv_correct), "mv_total": int(mv_total),
            "fp_count": int(fp), "fp_tile_frames": int(fp_total),
            "fp_ppm": None if fp_rate is None else int(fp_rate),
            "fp_ppm_exact": None if fp_rate is None else str(fp_rate),
            "engine_ms": engine_ms, "generator_ms": generator_ms,
            "ms_per_frame": flat["ms_per_frame"],
            "generator_ms_per_frame": flat["generator_ms_per_frame"], "frames": int(frames)}


def _verify_prereg(split, digest):
    if split not in ("DEV", "GATE", "TEST"):
        raise ScoreError("split_leak", split)
    if split == "DEV" and digest is None:
        return
    from . import prereg
    prereg.verify()
    if not digest or digest != prereg.sha256():
        raise ScoreError("E_PREREG_SHA", "result does not carry current seal")


def _legacy(cls, events):
    """The score_w3 first-eight-frame event view; never replaces exact scoring."""
    rows = []
    for event in events:
        start = int(event["f0"])
        end = cls.shape[0] if "until_us" in event else min(cls.shape[0], start + 8)
        tiles = event["tiles"]
        win = cls[start:end][:, tiles]
        hist = np.bincount(win.ravel().astype(np.int64), minlength=7)
        target = event.get("expected_kind", NAMES[_class(event["ideal"])])
        allowed = _LEGACY.get(target)
        if allowed is None:
            raise ScoreError("E_SCORE_LABEL", "unknown legacy expected_kind")
        total = int(hist.sum())
        hits = sum(int(hist[i]) for i in allowed)
        changes = int(hist[2:].sum())
        rows.append({"event_id": event.get("event_id"), "frame": start,
                     "target": event.get("target"), "op": event.get("op"),
                     "kind": target, "tiles": len(tiles), "detected": bool(changes),
                     "hit_frac": round(hits / (total or 1), 3),
                     "any_change_frac": round(changes / (total or 1), 3),
                     "hist": {NAMES[i]: int(hist[i]) for i in range(7) if hist[i]}})
    return rows


def score(cls, mv_dx, mv_dy, events, *, scene=None, scene_id=None, kind=None,
          split="DEV", prereg_sha=None, frame_shas=None, engine_ms=0,
          generator_ms=0):
    """Score one scene; timings are total milliseconds, not per-frame values.

    A scene_id (or scene containing it) is mandatory so foreign labels cannot
    silently become this scene's ground truth. Optional frame SHA witnesses are
    checked whenever the scene or events carry them.
    """
    _verify_prereg(split, prereg_sha)
    cls = _array(cls, classes=True)
    dx, dy = _array(mv_dx, cls.shape), _array(mv_dy, cls.shape)
    scene = scene or {}
    scene_id = scene_id or scene.get("scene_id")
    kind = kind or scene.get("kind")
    if not isinstance(scene_id, str) or not scene_id or not kind:
        raise ScoreError("E_SCORE_SCENE", "scene identity and kind required")
    if scene.get("scene_id", scene_id) != scene_id or scene.get("kind", kind) != kind:
        raise ScoreError("E_SCORE_SCENE", "scene metadata mismatch")
    frames, tiles = cls.shape
    if scene.get("n", frames) != frames:
        raise ScoreError("E_SCORE_SCENE", "frame count mismatch")
    for timing in (engine_ms, generator_ms):
        if isinstance(timing, bool) or not isinstance(timing, (int, float)) or not math.isfinite(timing) or timing < 0:
            raise ScoreError("E_SCORE_TIME", "finite nonnegative milliseconds required")
    events = list(events)
    if frame_shas is not None:
        frame_shas = list(frame_shas)
        if len(frame_shas) != frames or any(not isinstance(s, str) or len(s) != 64 for s in frame_shas):
            raise ScoreError("E_SCORE_SCENE", "invalid frame SHA table")
    witnesses = [scene] + events
    for witness in witnesses:
        if witness.get("scene_id", scene_id) != scene_id:
            raise ScoreError("E_SCORE_SCENE", "foreign label scene")
        for key in ("frame_shas", "sha_list"):
            if key in witness and (frame_shas is None or list(witness[key]) != frame_shas):
                raise ScoreError("E_SCORE_SCENE", "frame SHA mismatch")
    ideal = np.zeros(cls.shape, dtype=np.uint8)
    expected = np.zeros(cls.shape, dtype=np.uint8)
    mx = np.zeros(cls.shape, dtype=np.int64)
    my = np.zeros(cls.shape, dtype=np.int64)
    covered = np.zeros(cls.shape, dtype=bool)
    hypotheses = []
    for e in events:
        if e.get("scene_id") != scene_id:
            raise ScoreError("E_SCORE_SCENE", "label missing or foreign scene_id")
        f0, f1 = _integer(e["f0"]), _integer(e["f1"])
        if not 0 <= f0 < f1 <= frames:
            raise ScoreError("E_SCORE_LABEL", "invalid half-open interval")
        ids = [_integer(x) for x in e["tiles"]]
        if len(ids) != len(set(ids)) or any(x < 0 or x >= tiles for x in ids):
            raise ScoreError("E_SCORE_LABEL", "invalid tile indices")
        truth, v1 = _class(e["ideal"]), _class(e["v1_expected"])
        basis = e.get("v1_basis")
        if basis not in ("MEASURED-W3", "ER-014", "HYP"):
            raise ScoreError("E_SCORE_LABEL", "v1_basis required")
        motion = e.get("mv")
        if truth == 3 and (not isinstance(motion, (tuple, list)) or len(motion) != 2):
            raise ScoreError("E_SCORE_LABEL", "MOVED requires exact mv")
        motion = (0, 0) if motion is None else tuple(_integer(x) for x in motion)
        if len(motion) != 2:
            raise ScoreError("E_SCORE_LABEL", "invalid mv")
        ix = np.ix_(np.arange(f0, f1), ids)
        if covered[ix].any():
            raise ScoreError("E_SCORE_LABEL", "overlapping tile-frame labels")
        ideal[ix], expected[ix], mx[ix], my[ix], covered[ix] = truth, v1, *motion, True
        if basis == "HYP":
            mismatches = int((cls[ix] != v1).sum())
            if mismatches:
                hypotheses.append({"event_id": e.get("event_id"), "mismatches": mismatches,
                                   "basis": "HYP", "status": "finding"})
    confusion = np.bincount((ideal.astype(np.int64) * 7 + cls.astype(np.int64)).ravel(), minlength=49).reshape(7, 7)
    confusion_v1 = np.bincount((expected.astype(np.int64) * 7 + cls.astype(np.int64)).ravel(), minlength=49).reshape(7, 7)
    moved = ideal == 3
    mv_correct = int((moved & (cls == 3) & (dx == mx) & (dy == my)).sum())
    fp_total = cls.size if kind in ("static_clean", "aa_jitter") else 0
    fp = int((cls >= 2).sum()) if fp_total else 0
    result = _metrics(confusion, mv_correct, int(moved.sum()), fp, fp_total,
                      engine_ms, generator_ms, frames)
    result["metrics"]["ms_per_frame_1080p"] = (engine_ms / frames if
        (scene.get("w"), scene.get("h")) == (1920, 1080) else None)
    result.update(schema="vqa.gui_synth.result/v1", scene_id=scene_id, kind=kind,
                  split=split, prereg_sha=prereg_sha, class_names=NAMES,
                  resolution=[scene.get("w"), scene.get("h")],
                  tile_frames=int(cls.size), confusion_ideal=confusion.tolist(),
                  confusion_v1_expected=confusion_v1.tolist(),
                  v1_expected=_metrics(confusion_v1, 0, 0, fp, fp_total, engine_ms,
                                       generator_ms, frames),
                  legacy=_legacy(cls, events), hypothesis_findings=hypotheses,
                  frame_shas=frame_shas,
                  frame_sha_table_sha256=None if frame_shas is None else
                  hashlib.sha256("".join(s + "\n" for s in frame_shas).encode()).hexdigest())
    # A resolution-independent param_bucket is supplied by the scene generator.
    # Small hand-written fixtures still get a shared (not per-seed) parameter key.
    bucket = scene.get("param_bucket", {key: scene[key] for key in
        ("dx", "dy", "half_period_ms", "fps", "easing", "boxes") if key in scene})
    json.dumps(bucket, sort_keys=True, allow_nan=False)  # refuse non-JSON identity
    result["scene_units"] = {scene_id: {"kind": kind, "param_bucket": bucket,
                                        "metrics": result["metrics"]}}
    result["judge_view"] = judge_view(result)
    return result


score_scene = score


def aggregate(results):
    """Pool ideal counts, retaining scenes and their parameter strata."""
    results = list(results)
    if not results:
        raise ScoreError("E_SCORE_EMPTY")
    hashes = {r.get("prereg_sha") for r in results}
    if len(hashes) != 1:
        raise ScoreError("E_PREREG_SHA", "mixed seals")
    units = {}
    for r in results:
        for sid, unit in r["scene_units"].items():
            if sid in units:
                raise ScoreError("E_SCORE_SCENE", "duplicate scene")
            units[sid] = unit
    confusion = np.sum([r["confusion_ideal"] for r in results], axis=0)
    v1 = np.sum([r["confusion_v1_expected"] for r in results], axis=0)
    sums = {key: sum(r[key] for r in results) for key in
            ("mv_correct", "mv_total", "fp_count", "fp_tile_frames", "engine_ms", "generator_ms", "frames")}
    result = _metrics(confusion, sums["mv_correct"], sums["mv_total"], sums["fp_count"],
                      sums["fp_tile_frames"], sums["engine_ms"], sums["generator_ms"], sums["frames"])
    kinds = sorted({r["kind"] for r in results})
    per_kind = {}
    for kind in kinds:
        rr = [r for r in results if r["kind"] == kind]
        ss = {key: sum(r[key] for r in rr) for key in sums}
        per_kind[kind] = _metrics(np.sum([r["confusion_ideal"] for r in rr], axis=0),
                                 ss["mv_correct"], ss["mv_total"], ss["fp_count"],
                                 ss["fp_tile_frames"], ss["engine_ms"], ss["generator_ms"], ss["frames"])
        per_kind[kind]["confusion_ideal"] = per_kind[kind]["confusion"]
        per_kind[kind]["confusion_v1_expected"] = np.sum(
            [r["confusion_v1_expected"] for r in rr], axis=0).tolist()
    result.update(schema="vqa.gui_synth.result/v1", class_names=NAMES,
                  prereg_sha=next(iter(hashes)), splits=sorted({r["split"] for r in results}),
                  confusion_ideal=confusion.tolist(), confusion_v1_expected=v1.tolist(),
                  v1_expected=_metrics(v1, 0, 0, sums["fp_count"], sums["fp_tile_frames"],
                                       sums["engine_ms"], sums["generator_ms"], sums["frames"]),
                  tile_frames=sum(r["tile_frames"] for r in results), scene_units=units,
                  per_kind=per_kind, scenes=len(units),
                  hypothesis_findings=[dict(finding, scene_id=r["scene_id"])
                      for r in results for finding in r.get("hypothesis_findings", [])])
    result["judge_view"] = judge_view(result)
    return result


def judge_view(result):
    """Ideal-only judge evidence; v1 HYP mismatches are findings, never gates."""
    return {"confusion_ideal": result["confusion_ideal"],
            "metrics": result["metrics"],
            "per_class": result["per_class"],
            "support_moved": result["support_moved"],
            "findings": result.get("hypothesis_findings", [])}
