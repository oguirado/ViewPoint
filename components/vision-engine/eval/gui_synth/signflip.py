"""Paired two-sided comparisons; claims use kind/parameter stratum means."""
from collections import defaultdict
from fractions import Fraction
import json
from eval.hb_router.compare import sign_flip_p
from .score import ScoreError


def paired_p(deltas):
    return sign_flip_p([Fraction(x) for x in deltas])


def _fraction_text(value):
    return str(value.numerator) + "/" + str(value.denominator)


def compare(result_a, result_b, metric, kinds=None, split=None):
    # The bare mathematical fixture API has no correlated scene metadata.
    fixture = all(set(r) == {"units"} for r in (result_a, result_b))
    digest = None
    if not fixture:
        from . import prereg
        prereg.verify()
        digest = prereg.sha256()
        if any(r.get("prereg_sha", r.get("prereg_sha256")) != digest for r in (result_a, result_b)):
            raise ScoreError("E_PREREG_SHA", "comparison requires current seal on both results")

    def units(result):
        if fixture:
            return {sid: {"metrics": metrics, "param_bucket": sid}
                    for sid, metrics in result["units"].items()}
        return result.get("scene_units", {})

    a, b = units(result_a), units(result_b)
    if split is not None:
        a = {sid: u for sid, u in a.items() if sid.startswith(split + ":")}
        b = {sid: u for sid, u in b.items() if sid.startswith(split + ":")}
    selected = None if kinds is None else ({kinds} if isinstance(kinds, str) else set(kinds))
    if selected is not None:
        a = {sid: u for sid, u in a.items() if u.get("kind") in selected}
        b = {sid: u for sid, u in b.items() if u.get("kind") in selected}
    if set(a) != set(b) or (not a and selected is None):
        raise ScoreError("E_SCORE_SCENE", "paired scene sets must match exactly")
    deltas, strata = [], defaultdict(list)
    dropped = 0
    for sid in sorted(a):
        if a[sid].get("kind") != b[sid].get("kind"):
            raise ScoreError("E_SCORE_SCENE", "paired kind mismatch")
        # Never silently replace absent parameter evidence with independent scenes.
        if "param_bucket" not in a[sid] or "param_bucket" not in b[sid]:
            raise ScoreError("E_SCORE_SCENE", "parameter stratum required")
        bucket_a = json.dumps(a[sid]["param_bucket"], sort_keys=True, separators=(",", ":"), allow_nan=False)
        bucket_b = json.dumps(b[sid]["param_bucket"], sort_keys=True, separators=(",", ":"), allow_nan=False)
        if bucket_a != bucket_b:
            raise ScoreError("E_SCORE_SCENE", "paired parameter mismatch")
        try:
            av, bv = a[sid]["metrics"][metric], b[sid]["metrics"][metric]
            if av is None and bv is None:
                dropped += 1
                continue
            if av is None or bv is None:
                raise ValueError("metric defined on only one side")
            delta = Fraction(str(bv)) - Fraction(str(av))
        except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
            raise ScoreError("E_SCORE_METRIC", metric) from exc
        deltas.append(delta)
        strata[(a[sid].get("kind"), bucket_a)].append(delta)
    means = [sum(values, Fraction(0)) / len(values) for values in strata.values()]
    p_scene = sign_flip_p(deltas) if deltas else None
    p = sign_flip_p(means) if means else None
    mean_delta = sum(means, Fraction(0)) / len(means) if means else None
    scene_mean = sum(deltas, Fraction(0)) / len(deltas) if deltas else None
    lower_better = metric in ("fp_ppm", "ms_per_frame", "generator_ms_per_frame", "ms_per_frame_1080p")
    claim = "no claim"
    if split in ("GATE", "TEST") and p is not None and p < Fraction(1, 20) and mean_delta:
        claim = "improvement" if (mean_delta < 0 if lower_better else mean_delta > 0) else "regression"
    return {"schema": "vqa.gui_synth.comparison/v1", "prereg_sha": digest,
            "metric": metric, "split": split, "kinds": None if selected is None else sorted(selected),
            "unit": "kind_parameter_stratum", "n": len(deltas),
            "n_used": len(deltas), "n_dropped": dropped, "n_strata": len(means),
            "nonzero": sum(d != 0 for d in deltas),
            "p": None if p is None else _fraction_text(p),
            "p_exact": None if p is None else _fraction_text(p),
            "p_stratified": None if p is None else _fraction_text(p),
            "p_scene": None if p_scene is None else _fraction_text(p_scene),
            "mean_delta": None if mean_delta is None else str(mean_delta),
            "scene_mean_delta": None if scene_mean is None else str(scene_mean),
            "alpha": "1/20", "two_sided": True, "claim": claim,
            "p_method": "exact up to 20 nonzero units; seeded Monte Carlo thereafter"}
