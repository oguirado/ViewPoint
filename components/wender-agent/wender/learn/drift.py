# @module {"id":"wa.learn.drift","layer":2,"owner":"LEARN","deps":["wa.perceive.ax","wa.perceive.srf_identity"],"book":"HB-WA-s4-learn"}
"""Name-free drift gates and bounded, mergeable escalation summaries."""
import copy
import re
import uuid
from wender.perceive.ax import CHASSIS_ROLES
from wender.perceive.srf_identity import _eligible

LANDMARK_VOCAB = frozenset(('sidebar', 'main', 'composer', 'menu', 'dialog', 'header', 'nav', 'footer'))
REGION_TO_LANDMARK = {k: k for k in LANDMARK_VOCAB}
REGION_TO_LANDMARK.update(navigation='sidebar', banner='header', contentinfo='footer')
DRIFT_WINDOW_CYCLES = 20  # [TARGET]
DRIFT_RATE_NUM, DRIFT_RATE_DEN = 1, 4  # [TARGET]
MAX_CONSECUTIVE_UNKNOWN = 3  # [TARGET]
VERDICT_OK, VERDICT_UI_DRIFT, VERDICT_UNKNOWN = 'OK', 'UI_DRIFT', 'UNKNOWN'
_OUTCOMES = frozenset(('ok', 'abstain', 'unknown_screen', 'postcheck_failed_twice'))


def _landmark(value):
    if not isinstance(value, str):
        return None
    if re.fullmatch(r'answer:(?:[0-9]+|last)', value):
        return value
    return REGION_TO_LANDMARK.get(value)


# @anchor:wa.learn.drift.normalize_landmark
def normalize_landmark(element: dict) -> str | None:
    region = element.get('region')
    landmark = _landmark(region)
    if landmark:
        if (landmark == 'main' and element.get('role') in ('textbox', 'searchbox')
                and (element.get('container') or {}).get('id') in (None, 'composer')):
            return 'composer'
        return landmark
    for ancestor in element.get('ancestors') or ():
        landmark = _landmark(ancestor.get('role') if isinstance(ancestor, dict) else ancestor)
        if landmark:
            return landmark
    box = element.get('box')
    if region in (None, '') and isinstance(box, list) and len(box) == 4 and type(box[0]) is int:
        return 'sidebar' if box[0] < 240 else 'main'  # [CITED] architecture landmark split
    return None


# @anchor:wa.learn.drift.current_signature
def current_signature(ax_or_elements) -> frozenset[str]:
    elements = ax_or_elements.get('elements', []) if isinstance(ax_or_elements, dict) else ax_or_elements
    result = set()
    for element in elements:
        if not isinstance(element, dict):
            raise ValueError('invalid anchor element')
        synthetic = 'actionable' not in element and set(element) <= {'role', 'landmark'}
        if not synthetic and not _eligible(element):
            continue
        role = element.get('role')
        landmark = _landmark(element.get('landmark')) or normalize_landmark(element)
        if role in CHASSIS_ROLES and landmark:
            result.add(f'{role}|{landmark}')
    return frozenset(result)


# @anchor:wa.learn.drift.stored_signatures
def stored_signatures(memory_or_specs) -> tuple[frozenset[str], frozenset[str]]:
    specs = memory_or_specs.get('workflow', {}).get('anchor_signatures', []) if isinstance(memory_or_specs, dict) else memory_or_specs
    if not isinstance(specs, (list, tuple, set, frozenset)):
        raise ValueError('invalid anchor signatures')
    tokens, required = set(), set()
    for spec in specs:
        if isinstance(spec, str):
            parts = spec.split('|')
            if len(parts) != 2:
                raise ValueError('invalid anchor signature token')
            role, landmark = parts
            needed = False
        elif isinstance(spec, dict):
            if set(spec) - {'role', 'landmark', 'required'} or type(spec.get('required', False)) is not bool:
                raise ValueError('invalid anchor signature token')
            role, landmark, needed = spec.get('role'), spec.get('landmark'), spec.get('required', False)
        else:
            raise ValueError('invalid anchor signature token')
        landmark = _landmark(landmark)
        if not isinstance(role, str) or role not in CHASSIS_ROLES or not landmark:
            raise ValueError('invalid anchor signature token')
        token = f'{role}|{landmark}'
        tokens.add(token)
        if needed:
            required.add(token)
    return frozenset(tokens), frozenset(required)


# @anchor:wa.learn.drift.jaccard_num_den
def jaccard_num_den(stored: frozenset[str], current: frozenset[str]) -> tuple[int, int]:
    if not stored or not current:
        return 0, 0
    return len(stored & current), len(stored | current)


# @anchor:wa.learn.drift.check_drift
def check_drift(stored_input, current_input) -> dict:
    stored, required = stored_signatures(stored_input)
    current = stored_signatures(current_input)[0] if isinstance(current_input, (set, frozenset)) else current_signature(current_input)
    if not stored or not current:
        return dict(verdict=VERDICT_UNKNOWN, jaccard_num=0, jaccard_den=0, vanished=[],
                    reground_required=True, missing_reason='empty_anchor_set')
    # A coarse stored main anchor can use a refined composer observation.
    if any(t.endswith('|main') and t.split('|')[0] in ('textbox', 'searchbox') for t in stored):
        current = frozenset(t.replace('|composer', '|main') if t.replace('|composer', '|main') in stored else t for t in current)
    vanished = sorted(required - current)
    num, den = jaccard_num_den(stored, current)
    drift = bool(vanished or 2 * num < den)  # [TARGET] strict half threshold
    return dict(verdict=VERDICT_UI_DRIFT if drift else VERDICT_OK, jaccard_num=num,
                jaccard_den=den, vanished=vanished, reground_required=drift, missing_reason=None)


def _receipt_identity(site, memory_version, intent):
    # Receipt identifiers are public protocol labels, never arbitrary observation text.
    if (not isinstance(site, str) or re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', site) is None
            or type(memory_version) is not int or memory_version < 1
            or not isinstance(intent, str) or (intent and re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', intent) is None)):
        raise ValueError('invalid receipt identity')


# @anchor:wa.learn.drift.invalidate_macros
def invalidate_macros(macros: dict, drift_result: dict, *, site: str = 'perplexity', memory_version: int = 2, intent: str = '') -> tuple[dict, dict | None]:
    if not isinstance(macros, dict) or not isinstance(drift_result, dict):
        raise ValueError('invalid macro invalidation input')
    _receipt_identity(site, memory_version, intent)
    verdict = drift_result.get('verdict')
    if verdict not in (VERDICT_OK, VERDICT_UNKNOWN, VERDICT_UI_DRIFT):
        raise ValueError('invalid drift verdict')
    if verdict != VERDICT_UI_DRIFT:
        return copy.deepcopy(macros), None
    num, den = drift_result.get('jaccard_num'), drift_result.get('jaccard_den')
    vanished = drift_result.get('vanished')
    if type(num) is not int or type(den) is not int or not 0 <= num <= den or den == 0 or not isinstance(vanished, list):
        raise ValueError('invalid drift evidence')
    safe_vanished = sorted(stored_signatures(vanished)[0])
    return {}, dict(event='UI_DRIFT', site=site, memory_version=memory_version, intent=intent,
                    jaccard_num=num, jaccard_den=den, vanished=safe_vanished,
                    invalidated_macros=len(macros), action='invalidate_macros_and_reground')


# @anchor:wa.learn.drift.guard_macro_step
def guard_macro_step(macros: dict, stored_input, current_input, *, intent: str, site: str = 'perplexity', memory_version: int = 2) -> dict:
    result = check_drift(stored_input, current_input)
    new_macros, receipt = invalidate_macros(macros, result, site=site, memory_version=memory_version, intent=intent)
    allow = result['verdict'] == VERDICT_OK and intent in new_macros
    return dict(result, macros=new_macros, receipt=receipt, allow_cached_macro=allow,
                cached_element_id=new_macros[intent] if allow else None, known=allow)


# @anchor:wa.learn.drift.DriftTally
class DriftTally:
    """Bounded per-key event summaries with deterministic logical merge order.

    Events use (Lamport sequence, source id) order. A single writer retains real
    cycle order; concurrent sources have no real-time order guarantee. Top-k set
    union is associative, commutative and idempotent, with the empty identity.
    """
    def __init__(self):
        self._source = uuid.uuid4().hex
        self._clock = 0
        self._history = {}

    # @anchor:wa.learn.drift.DriftTally.record
    def record(self, site, memory_version, intent, outcome) -> dict | None:
        _receipt_identity(site, memory_version, intent)
        if outcome not in _OUTCOMES:
            raise ValueError('invalid cycle outcome')
        key = site, memory_version, intent
        self._clock += 1
        history = self._history.get(key, ()) + ((self._clock, self._source, outcome),)
        history = history[-DRIFT_WINDOW_CYCLES:]
        self._history[key] = history
        votes = sum(row[2] in ('abstain', 'unknown_screen') for row in history)
        consecutive = 0
        for row in reversed(history):
            if row[2] != 'unknown_screen':
                break
            consecutive += 1
        if consecutive >= MAX_CONSECUTIVE_UNKNOWN or (len(history) == DRIFT_WINDOW_CYCLES and DRIFT_RATE_DEN * votes > DRIFT_RATE_NUM * len(history)):
            return dict(event='UI_DRIFT', reason='escalation_rate_or_consecutive_unknown',
                        site=site, memory_version=memory_version, intent=intent,
                        window_count=len(history), drift_votes=votes, consecutive_unknown=consecutive,
                        freeze_promotion=True)
        return None

    # @anchor:wa.learn.drift.DriftTally.merge
    def merge(self, other):
        if not isinstance(other, DriftTally):
            raise ValueError('invalid drift tally')
        merged = DriftTally()
        merged._clock = max(self._clock, other._clock)
        for key in self._history.keys() | other._history.keys():
            merged._history[key] = tuple(sorted(set(self._history.get(key, ())) | set(other._history.get(key, ())))[-DRIFT_WINDOW_CYCLES:])
        return merged
