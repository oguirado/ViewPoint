# @module {"id":"wa.learn.calibrate","layer":2,"owner":"LEARN","deps":["wa.learn.script_policy"],"book":"HB-WA-s4-learn"}
"""RAM-only calibration. Origin: fastexec/bench/run_model.py:69-106,
fastexec/bench/score.py:80-104; perceive/holdings_abstention.py result contract.
Frozen defaults are targets, never fitted artifacts from the R4 benchmark.
"""
from dataclasses import dataclass, asdict, field
import hashlib
import json
import math
from typing import Mapping, Sequence
from .script_policy import validate_calibration_artifact as _validate_artifact

CALIBRATED, ABSTAIN, BLOCKED = 'CALIBRATED', 'ABSTAIN', 'BLOCKED'
# @anchor:wa.learn.calibrate.CALIBRATION_CODES
FAILURE_CODES = frozenset('DISTRIBUTION_UNKNOWN LOGPROBS_MISSING SELF_REPORT_CONFIDENCE_REJECTED MULTI_TOKEN_KEY NONFINITE_LOGPROB TRUNCATED_SUPPORT UNNORMALIZED_SUM ARGMAX_MISMATCH SERVING_GATE_FAILED MODEL_UNCALIBRATED CALIBRATION_SPLIT_ABSENT DEGENERATE_LOGITS ECE_ABOVE_THRESHOLD ESCALATION_RATE_ABOVE_THRESHOLD COVERAGE_BELOW_THRESHOLD BELOW_TAU BELOW_DESTRUCTIVE_TAU LOW_MARGIN NON_SINGLETON_CONFORMAL_SET IRREVERSIBLE_LEVEL_DENIED CODIGO_DESCONOCIDO'.split())
MIN_CAL_SAMPLES = 500  # [TARGET]
TAU_READ_PROMILLE, TAU_DEFAULT_PROMILLE = 600, 800  # [CITED], [TARGET]
TAU_MUTATING_PROMILLE, TAU_DESTRUCTIVE_PROMILLE = 850, 950  # [CITED], [TARGET]
HYSTERESIS_PROMILLE, MARGIN_PROMILLE = 20, 150  # [TARGET]
MAX_ECE_PROMILLE, MAX_ESCALATION_PROMILLE = 50, 400  # [TARGET]
CONFORMAL_ALPHA_PROMILLE, SUM_TOLERANCE_PPM = 50, 1000  # [TARGET], [CITED]

# @anchor:wa.learn.calibrate.DistributionReceipt
@dataclass(frozen=True, slots=True)
class DistributionReceipt:
    verdict: str
    failure_code: str | None = None
    generated_key: str | None = None
    target_id: str | None = None
    top1_promille: int = 0
    top2_promille: int = 0
    margin_promille: int = 0
    probs_ppm: Mapping = field(default_factory=dict)
    probs_promille: Mapping = field(default_factory=dict)
    logits_milli: Mapping = field(default_factory=dict)

    # @anchor:wa.learn.calibrate.DistributionReceipt.metadata
    def metadata(self):
        return asdict(self)

# @anchor:wa.learn.calibrate.CalibrationResult
@dataclass(frozen=True, slots=True)
class CalibrationResult:
    verdict: str
    failure_code: str | None = None
    cal_id: str = ''
    t_star_num: int = 1000
    t_star_den: int = 1000
    q_hat_promille: int | None = None
    ece_promille: int | str = 'UNAVAILABLE'
    brier_ppm: int | str = 'UNAVAILABLE'
    escalation_rate_promille: int | str = 'UNAVAILABLE'
    tau_default_promille: int = TAU_DEFAULT_PROMILLE
    tau_destructive_promille: int = TAU_DESTRUCTIVE_PROMILLE
    margin_promille: int = MARGIN_PROMILLE
    n_cal: int = 0
    bins: tuple = ()
    status: str = 'FROZEN_DEFAULT'
    top1_promille: int = 0
    top2_promille: int = 0
    conformal_keys: tuple = ()
    target_id: str | None = None
    calibration_gate_passed: bool = False

    # @anchor:wa.learn.calibrate.CalibrationResult.metadata
    def metadata(self):
        result = asdict(self)
        result['bins'] = list(result['bins'])
        result['conformal_keys'] = list(result['conformal_keys'])
        return result

# @anchor:wa.learn.calibrate.verify_distribution
def verify_distribution(mapping, logprobs_content, generated_key, *, serving_gates=None, self_reported_confidence=None):
    def reject(code):
        return DistributionReceipt(ABSTAIN, code)
    if not logprobs_content:
        return reject('SELF_REPORT_CONFIDENCE_REJECTED' if logprobs_content is None and self_reported_confidence is not None else 'LOGPROBS_MISSING')
    if (not mapping or len(mapping) > 20 or any(not isinstance(k, str) or len(k) != 1 or not 'A' <= k <= 'T' for k in mapping)
            or not isinstance(logprobs_content, (list, tuple)) or len(logprobs_content) != 1):
        return reject('MULTI_TOKEN_KEY')
    if serving_gates is not None:
        if (not isinstance(serving_gates, Mapping) or not isinstance(serving_gates.get('grammar_mask'), (list, tuple, set, frozenset))
                or set(serving_gates.get('grammar_mask', ())) != set(mapping)
                or serving_gates.get('logprobs_mode') != 'processed_logprobs'
                or not serving_gates.get('model_sha') or not serving_gates.get('tokenizer_sha')):
            return reject('SERVING_GATE_FAILED')
    probs, logits = {}, {}
    try:
        content = logprobs_content[0]
        if content.get('token') != generated_key or not isinstance(generated_key, str) or len(generated_key) != 1:
            return reject('MULTI_TOKEN_KEY')
        for row in content['top_logprobs']:
            key, lp = row['token'], row['logprob']
            if key not in mapping or key in probs:
                return reject('TRUNCATED_SUPPORT')
            if type(lp) not in (int, float) or not math.isfinite(lp) or lp > 0:
                return reject('NONFINITE_LOGPROB')
            probs[key], logits[key] = math.exp(lp), math.floor(lp * 1000)
    except (KeyError, TypeError, AttributeError, OverflowError):
        return reject('DISTRIBUTION_UNKNOWN')
    if set(probs) != set(mapping):
        return reject('TRUNCATED_SUPPORT')
    if abs(sum(probs.values()) - 1) > SUM_TOLERANCE_PPM / 1000000:
        return reject('UNNORMALIZED_SUM')
    if generated_key not in probs or probs[generated_key] < max(probs.values()) - 1e-12:
        return reject('ARGMAX_MISMATCH')
    ordered = sorted(probs.values(), reverse=True)
    pm = {k: math.floor(p * 1000 + 1e-9) for k, p in probs.items()}
    top, second = pm[generated_key], math.floor((ordered[1] if len(ordered) > 1 else 0) * 1000 + 1e-9)
    return DistributionReceipt(CALIBRATED, None, generated_key, mapping[generated_key], top, second, top-second,
                               {k: round(p * 1000000) for k, p in probs.items()}, pm, logits)


def _scaled(logits, num, den):
    if type(num) is not int or type(den) is not int or min(num, den) <= 0 or not logits:
        raise ValueError('invalid temperature or logits')
    if any(type(z) is not int for z in logits.values()):
        raise ValueError('millilogits must be integers')
    values = {k: z * den / (1000 * num) for k, z in logits.items()}
    peak = max(values.values())
    weights = {k: math.exp(v - peak) for k, v in values.items()}
    total = sum(weights.values())
    return {k: p / total for k, p in weights.items()}

# @anchor:wa.learn.calibrate.fit_temperature
def fit_temperature(cal_rows, *, min_samples=MIN_CAL_SAMPLES):
    if len(cal_rows) < max(MIN_CAL_SAMPLES, min_samples):
        return 1000, 1000, 'CALIBRATION_SPLIT_ABSENT'
    if (len({r['true_key'] for r in cal_rows}) < 2
            or all(len(set(r['logits_milli'].values())) <= 1 for r in cal_rows)):
        return 1000, 1000, 'DEGENERATE_LOGITS'
    def score(num):
        loss = sum(round(-math.log(max(_scaled(r['logits_milli'], num, 1000)[r['true_key']], 1e-300)) * 1000000) for r in cal_rows)
        return loss, abs(num - 1000), num
    best = min(range(100, 5001, 25), key=score)
    return best, 1000, None

# @anchor:wa.learn.calibrate.conformal_quantile
def conformal_quantile(cal_rows, t_star_num, t_star_den, *, alpha_promille=CONFORMAL_ALPHA_PROMILLE, min_samples=MIN_CAL_SAMPLES):
    if len(cal_rows) < max(MIN_CAL_SAMPLES, min_samples):
        return None, 'CALIBRATION_SPLIT_ABSENT'
    if type(alpha_promille) is not int or not 0 < alpha_promille < 1000:
        raise ValueError('invalid alpha')
    scores = sorted(1000 - math.floor(_scaled(r['logits_milli'], t_star_num, t_star_den)[r['true_key']] * 1000) for r in cal_rows)
    index = ((len(scores) + 1) * (1000 - alpha_promille) + 999) // 1000 - 1
    return (scores[index] if index < len(scores) else 1000), None

# @anchor:wa.learn.calibrate.conformal_set
def conformal_set(probs_T_promille, q_hat_promille):
    if type(q_hat_promille) is not int or not 0 <= q_hat_promille <= 1000 or any(type(p) is not int or not 0 <= p <= 1000 for p in probs_T_promille.values()):
        raise ValueError('invalid conformal probabilities')
    return tuple(k for k, p in sorted(probs_T_promille.items()) if p >= 1000 - q_hat_promille)

# @anchor:wa.learn.calibrate.ece_brier
def ece_brier(records, *, total_items=None):
    accepted = [r for r in records if not r.get('abstained', False) and r.get('accepted', True)]
    total = len(records) if total_items is None else total_items
    if (type(total) is not int or total < len(records) or total < len(accepted)
            or any(type(r.get('confidence_promille')) is not int or not 0 <= r['confidence_promille'] <= 1000
                   or type(r.get('correct')) is not bool for r in accepted)):
        raise ValueError('invalid total')
    bins, deviation = [], 0
    for i in range(10):
        rows = [r for r in accepted if min(9, r['confidence_promille'] // 100) == i]
        n, tp, conf = len(rows), sum(bool(r['correct']) for r in rows), sum(r['confidence_promille'] for r in rows)
        deviation += abs(1000 * tp - conf)
        bins.append(dict(decile=i, n=n, tp=tp, accuracy_promille=1000*tp//n if n else 'UNAVAILABLE',
                         confidence_promille=conf//n if n else 'UNAVAILABLE', laplace_conf_promille=1000*(tp+1)//(n+2) if n else 'UNAVAILABLE'))
    n = len(accepted)
    ece = round(deviation/n) if n else 'UNAVAILABLE'
    esc = round(1000*(total-n)/total) if total else 'UNAVAILABLE'
    code = ('ECE_ABOVE_THRESHOLD' if n and ece > MAX_ECE_PROMILLE else
            'ESCALATION_RATE_ABOVE_THRESHOLD' if total and esc > MAX_ESCALATION_PROMILLE else None)
    return dict(ece_promille=ece, brier_ppm=sum((r['confidence_promille']-1000*bool(r['correct']))**2 for r in accepted)//n if n else 'UNAVAILABLE',
                escalation_rate_promille=esc, bins=bins, calibration_gate_passed=bool(n and total and code is None), failure_code=code)

# @anchor:wa.learn.calibrate.clopper_pearson_upper_promille
def clopper_pearson_upper_promille(k_err, n_acc, alpha=0.05):
    if type(k_err) is not int or type(n_acc) is not int or not 0 <= k_err <= n_acc or not 0 < alpha < 1:
        raise ValueError('invalid binomial parameters')
    if n_acc == 0:
        return None
    if k_err == n_acc:
        return 1000
    def cdf(p):
        if p >= 1:
            return 0.0
        logs = [math.lgamma(n_acc+1)-math.lgamma(j+1)-math.lgamma(n_acc-j+1)+j*math.log(p)+(n_acc-j)*math.log1p(-p) for j in range(k_err+1)]
        peak = max(logs)
        return math.exp(peak)*sum(math.exp(x-peak) for x in logs)
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo+hi)/2
        if cdf(mid) > alpha:
            lo = mid
        else:
            hi = mid
    return math.ceil(hi*1000)

# @anchor:wa.learn.calibrate.select_thresholds
def select_thresholds(cal_rows, *, error_budget_promille=50, min_samples=MIN_CAL_SAMPLES):
    result = dict(tau_default_promille=800, tau_destructive_promille=950, status=BLOCKED, failure_code='CALIBRATION_SPLIT_ABSENT')
    if len(cal_rows) < max(MIN_CAL_SAMPLES, min_samples):
        return result
    valid = []
    for tau in range(500, 991, 10):
        rows = [r for r in cal_rows if r['confidence_promille'] >= tau]
        upper = clopper_pearson_upper_promille(sum(not r['correct'] for r in rows), len(rows))
        if upper is not None and upper <= error_budget_promille:
            valid.append(tau)
    if not valid:
        return {**result, 'failure_code': 'ECE_ABOVE_THRESHOLD'}
    chosen = min(valid)
    return dict(tau_default_promille=max(TAU_DEFAULT_PROMILLE, chosen),
                tau_destructive_promille=max(TAU_DESTRUCTIVE_PROMILLE, chosen), status=CALIBRATED, failure_code=None)


def _finish(code, **kwargs):
    if code is not None and code not in FAILURE_CODES:
        code = 'CODIGO_DESCONOCIDO'
    return CalibrationResult(ABSTAIN if code else CALIBRATED, code, **kwargs)

# @anchor:wa.learn.calibrate.calibrate_decision
def calibrate_decision(receipt, *, action_kind='', destructive_or_irreversible=False, match_level=None,
                       t_star_num=1000, t_star_den=1000, q_hat_promille=200,
                       model_sha=None, tokenizer_sha=None, serving_flags=None, split_sha=None, records=None,
                       automatic=False, artifact_context=None):
    if automatic:
        try:
            valid = type(artifact_context) is dict and _validate_artifact(**artifact_context)
        except Exception:
            valid = False
        if not valid:
            return _finish('MODEL_UNCALIBRATED')
        # Integrated math consumes only the registered fitted values.
        params = dict(artifact_context['artifact'].parameters)
        t_star_num, t_star_den = params['t_star_num'], params['t_star_den']
        q_hat_promille = params['q_hat_promille']
        a = artifact_context['artifact']
        if ((model_sha is not None and model_sha != a.model)
                or (tokenizer_sha is not None and tokenizer_sha != a.tokenizer)
                or (split_sha is not None and split_sha != a.split)
                or (serving_flags is not None and serving_flags != dict(a.serving))):
            return _finish('MODEL_UNCALIBRATED')
        model_sha, tokenizer_sha, split_sha = a.model, a.tokenizer, a.split
        serving_flags = dict(a.serving)
    model_sha = '' if model_sha is None else model_sha
    tokenizer_sha = '' if tokenizer_sha is None else tokenizer_sha
    split_sha = '' if split_sha is None else split_sha
    stable_flags = {k: v for k, v in (serving_flags or {}).items() if k in
                    ('grammar_mask', 'logprobs_mode', 'temperature', 'max_tokens', 'enable_thinking')}
    identity = [model_sha, tokenizer_sha, stable_flags, split_sha, t_star_num, t_star_den, q_hat_promille]
    cal_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
    n_cal = len(records) if records is not None else len(a.sample_refs) if automatic else 0
    if receipt.verdict != CALIBRATED:
        return _finish(receipt.failure_code or 'DISTRIBUTION_UNKNOWN', cal_id=cal_id, n_cal=n_cal)
    probs = (dict(receipt.probs_promille) if t_star_num == t_star_den else
             {k: math.floor(p*1000) for k, p in _scaled(receipt.logits_milli, t_star_num, t_star_den).items()})
    ranked = sorted(probs.values(), reverse=True)
    top, second = ranked[0], ranked[1] if len(ranked) > 1 else 0
    keys = conformal_set(probs, q_hat_promille)
    destructive = destructive_or_irreversible or action_kind in ('delete', 'submit_order', 'transfer', 'destructive')
    tau = 950 if destructive else 850 if action_kind in ('submit', 'type', 'key') else 600 if action_kind in ('read', 'focus') else 800
    margin = MARGIN_PROMILLE
    if automatic:
        thresholds = dict(a.thresholds)
        tau = max(tau, thresholds['tau_destructive_promille' if destructive else 'tau_default_promille'])
        margin = max(margin, thresholds['margin_promille'])
    code = None
    if destructive and top < tau:
        code = 'BELOW_DESTRUCTIVE_TAU'
    elif destructive and match_level not in ('L0_EXACT', 'L1_STABLE'):
        code = 'IRREVERSIBLE_LEVEL_DENIED'
    elif top-second < margin:
        code = 'LOW_MARGIN'
    elif len(keys) > 1:
        code = 'NON_SINGLETON_CONFORMAL_SET'
    elif top < tau or (not destructive and abs(top-tau) <= HYSTERESIS_PROMILLE):
        code = 'BELOW_TAU'
    elif len(keys) != 1 or receipt.generated_key not in keys:
        code = 'NON_SINGLETON_CONFORMAL_SET'
    metrics = ece_brier(records) if records is not None else {}
    if metrics.get('failure_code'):
        code = metrics['failure_code']
    return _finish(code, cal_id=cal_id, n_cal=n_cal, t_star_num=t_star_num, t_star_den=t_star_den, q_hat_promille=q_hat_promille,
                   top1_promille=top, top2_promille=second, margin_promille=top-second, conformal_keys=keys,
                   target_id=receipt.target_id, **{k: tuple(v) if k == 'bins' else v for k,v in metrics.items() if k != 'failure_code'})
