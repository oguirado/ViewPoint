# @module {"id":"wa.learn.cu_memory","layer":2,"owner":"LEARN","deps":["wa.learn.script_ledger"],"book":"HB-WA-s3-remember"}
"""Bounded, length-only execution memory and action evidence checks."""
from __future__ import annotations

from collections import deque, OrderedDict
from dataclasses import dataclass
import json
import re
from .script_ledger import project_receipt as _project_receipt


# @anchor:wa.learn.cu_memory.project_script_receipt
def project_script_receipt(owner, event, *, destination, purpose, approve, tokenizer, failure=False):
    """Return the owner-approved script schema as bytes, never legacy Receipt.

    The sealed ledger validates event registration, destination, actual consumer
    tokens and byte bounds. Do not hash, shorten or relabel private script values.
    """
    return _project_receipt(owner, event, destination=destination, purpose=purpose,
                            approve=approve, tokenizer=tokenizer, failure=failure)

RECEIPT_SCHEMA = 'cu.receipt/v1'
RING_CAPACITY = MAX_REFUSED = 8
LOCAL_HISTORY_CAP = 3
MAX_VISITED_HASHES = 12
RECEIPT_TOKEN_BUDGET = 120
HISTORY_TOKEN_BUDGET = 1200
FNV64_OFFSET_BASIS = 0xCBF29CE484222325
FNV64_PRIME = 0x100000001B3
FNV64_MASK = 0xFFFFFFFFFFFFFFFF
VALID_RESULTS = frozenset({'sealed', 'noop_done', 'noop_wrong', 'stagnant', 'refused', 'verify_fail', 'error'})


# @anchor:wa.learn.cu_memory.fnv1a_64_hex
def fnv1a_64_hex(data: bytes | str, *, width: int = 16) -> str:
    if not 1 <= width <= 16:
        raise ValueError('invalid hash width')
    h = FNV64_OFFSET_BASIS
    for byte in data.encode('utf-8') if isinstance(data, str) else data:
        h = ((h ^ byte) * FNV64_PRIME) & FNV64_MASK
    return f'{h:016x}'[:width]


def _label(value, limit=64):
    text = str(value)
    # Identifiers are structural data; prose and URLs become opaque digests.
    return text if len(text) <= limit and re.fullmatch(r'[A-Za-z0-9_.:/,+-]+', text) and '://' not in text else 'hash:' + fnv1a_64_hex(text)


# @anchor:wa.learn.cu_memory.canonical_snapshot_bytes
def canonical_snapshot_bytes(status: dict | None, elements: list[dict] | None = None) -> bytes:
    values = {k: (_label(v) if isinstance(v, str) else v)
              for k, v in (status or {}).items()
              if k not in ('last_refusal_code', 'status_text', 'focus', 'focused', 'bbox', 'box', 'text', 'value')
              and v is not None and isinstance(v, (str, int, bool))}
    payload = {'status': values}
    if elements is not None:
        payload['elements'] = sorted([
            (_label(e.get('id') or e.get('stable_id') or ''), _label(e.get('role') or ''),
             e.get('value_len') if type(e.get('value_len')) is int and e['value_len'] >= 0 else None,
             (e.get('states') or {}).get('enabled'), (e.get('states') or {}).get('busy'))
            for e in elements], key=lambda row: json.dumps(row, separators=(',', ':')))
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


# @anchor:wa.learn.cu_memory.canonical_action_sig
def canonical_action_sig(action: dict) -> str:
    kind = _label(action.get('kind', 'unknown'))
    target = 'none'
    for key, prefix in (('element_id', 'element'), ('mark_id', 'mark'), ('id', 'option')):
        if action.get(key) is not None:
            target = prefix + ':' + _label(action[key])
            break
    else:
        if isinstance(action.get('element'), dict):
            target = 'element:' + _label(action['element'].get('id') or 'anon')
        elif 'x' in action and 'y' in action:
            target = f"coord:{int(action['x'])},{int(action['y'])}"
    suffix = (_label(action.get('keysym', '')) if kind == 'key' else
              f"len:{len(str(action.get('text', '')))}" if kind == 'type' else
              f"ms:{int(action.get('ms', 0))}" if kind == 'wait' else '-')
    return f'{kind}:{target}:{suffix}'


# @anchor:wa.learn.cu_memory.Receipt
@dataclass(frozen=True)
class Receipt:
    step: int
    intent: str
    target_sig: str
    result: str
    verify_one_line: str
    snapshot_fnv64: str
    ok: bool = False
    option_id: str | None = None
    refusal_code: str | None = None
    dist_delta: int | None = None
    pre_state_hash: str | None = None

    # @anchor:wa.learn.cu_memory.Receipt.to_dict
    def to_dict(self) -> dict:
        if self.result not in VALID_RESULTS:
            raise ValueError('invalid receipt result')
        row = dict(schema=RECEIPT_SCHEMA, step=self.step, intent=_label(self.intent, 80),
                   target_sig=_label(self.target_sig, 96), result=self.result,
                   verify_one_line=_label(self.verify_one_line, 80), snapshot_fnv64=self.snapshot_fnv64)
        if self.refusal_code is not None:
            row['refusal_code'] = _label(self.refusal_code, 40)
        if self.dist_delta is not None:
            row['dist_delta'] = self.dist_delta
        for key in ('verify_one_line', 'intent', 'target_sig', 'refusal_code'):
            if len(json.dumps(row).encode('utf-8')) <= RECEIPT_TOKEN_BUDGET * 4:
                break
            if key in row:
                row[key] = 'hash:' + fnv1a_64_hex(row[key])
        if len(json.dumps(row).encode('utf-8')) > RECEIPT_TOKEN_BUDGET * 4:
            raise ValueError('receipt budget exceeded')
        return row

    # @anchor:wa.learn.cu_memory.Receipt.to_local_compact
    def to_local_compact(self) -> dict:
        row = self.to_dict()
        return dict(intent=row['intent'], id=_label(self.option_id or self.target_sig),
                    ok=bool(self.ok), result=self.result,
                    **({'refusal': row['refusal_code']} if self.refusal_code else {}))


# @anchor:wa.learn.cu_memory.WorkingMemoryRing
class WorkingMemoryRing:
    def __init__(self):
        self._ring = deque(maxlen=RING_CAPACITY)
        self.triples = deque(maxlen=RING_CAPACITY)
        self.visited_state_hashes = deque(maxlen=MAX_VISITED_HASHES)
        self.refused = OrderedDict()

    # @anchor:wa.learn.cu_memory.WorkingMemoryRing.append
    def append(self, receipt: Receipt | dict) -> Receipt:
        if isinstance(receipt, dict):
            receipt = Receipt(**{k: v for k, v in receipt.items() if k in Receipt.__dataclass_fields__})
        receipt.to_dict()
        self._ring.append(receipt)
        self.triples.append((receipt.intent, receipt.target_sig, receipt.pre_state_hash))
        self.visited_state_hashes.append(receipt.snapshot_fnv64[:12])
        if receipt.refusal_code:
            key = (receipt.target_sig, receipt.refusal_code)
            self.refused[key] = self.refused.pop(key, 0) + 1
            while len(self.refused) > MAX_REFUSED:
                self.refused.popitem(last=False)
        return receipt

    # @anchor:wa.learn.cu_memory.WorkingMemoryRing.last
    def last(self, k: int = RING_CAPACITY) -> list[dict]:
        return [r.to_dict() for r in list(self._ring)[-k:]] if k > 0 else []

    # @anchor:wa.learn.cu_memory.WorkingMemoryRing.for_muse
    def for_muse(self) -> list[dict]:
        return self.last()

    # @anchor:wa.learn.cu_memory.WorkingMemoryRing.for_local
    def for_local(self) -> list[dict]:
        return [r.to_local_compact() for r in list(self._ring)[-LOCAL_HISTORY_CAP:]]

    # @anchor:wa.learn.cu_memory.WorkingMemoryRing.clear
    def clear(self) -> None:
        self._ring.clear()
        self.triples.clear()
        self.visited_state_hashes.clear()
        self.refused.clear()


# @anchor:wa.learn.cu_memory.compute_loop_score
def compute_loop_score(triples, candidate_triple=None, *, visited_hashes=(), stagnant_steps=0,
                       consecutive_failures=0, repeated_refusal=False) -> int:
    candidate = candidate_triple or (triples[-1] if triples else None)
    count = sum(t[1:] == candidate[1:] for t in triples) if candidate else 0
    return max(count if count >= 2 else 0,
               stagnant_steps + int(consecutive_failures > 0) + 2 * int(repeated_refusal))


# @anchor:wa.learn.cu_memory.classify_step_outcome
def classify_step_outcome(*, changed, verify_ok, snapshot_revisited, predicate_before,
                          predicate_after, distance_before=None, distance_after=None, refusal_code=None):
    if (not changed or not verify_ok) and (predicate_before is True or predicate_after is True) and refusal_code is None:
        return 'already_done_noop'
    if not verify_ok:
        return 'wrong_target_noop'
    if snapshot_revisited or (distance_before is not None and distance_after is not None
                             and distance_after >= distance_before and not changed):
        return 'stagnant_verified'
    return 'sealed'


def _elements(frame):
    return frame.get('lane', frame.get('ax', frame)).get('elements', [])


def _target(action, elements):
    ident = action.get('element_id') or (action.get('element') or {}).get('id')
    if ident is not None:
        matches = [e for e in elements if (e.get('id') or e.get('stable_id')) == ident]
        if action.get('kind') == 'type':
            matches = [e for e in matches if e.get('role') in ('textbox', 'input', 'entry')]
        return matches[0] if len(matches) == 1 else None
    return next((e for e in elements if e.get('focused') or (e.get('states') or {}).get('focused')),
                next((e for e in elements if e.get('role') in ('textbox', 'input', 'entry')), None))


def _length(element):
    value = (element or {}).get('value_len')
    return value if type(value) is int and value >= 0 else None


# @anchor:wa.learn.cu_memory.verify_action_postcondition
def verify_action_postcondition(action, before, after, description=None, *, expectation=None):
    expectation = expectation or {}
    kind = expectation.get('kind')
    old, new = _elements(before), _elements(after)
    a, b = _target(action, old), _target(action, new)
    def result(method, ok):
        return dict(method=method, ok=bool(ok), detail='action evidence satisfied' if ok else 'action evidence missing or unchanged')
    if action.get('kind') == 'type' or kind == 'value_delta':
        x, y = _length(a), _length(b)
        if x is None or y is None:
            return result('value_delta', False) if kind == 'value_delta' else None
        return result('value_delta', y > x)
    submit_re = re.compile(r'(?i)(?:\b|[-_])(submit|send|enviar|ask|preguntar)(?:\b|[-_])')
    boxes_old = [e for e in old if e.get('role') in ('textbox', 'input', 'entry')]
    boxes_new = [e for e in new if e.get('role') in ('textbox', 'input', 'entry')]
    submit = kind == 'submit_seen' or action.get('kind') == 'submit' or (
        bool(boxes_old) and (action.get('kind') == 'key' and action.get('keysym') in ('Return', 'Enter', 'KP_Enter') or
        action.get('kind') == 'click' and a is not None and
        (a.get('role') == 'submit' or submit_re.search(str(a.get('name', '')) + ' ' + str(a.get('id', ''))))))
    if submit:
        old_by_id = {e.get('id'): e for e in boxes_old}
        cleared = any(_length(e) == 0 and (_length(old_by_id.get(e.get('id'))) or 0) > 0 for e in boxes_new)
        buttons = [e for e in old if e.get('role') == 'submit' or submit_re.search(str(e.get('name', '')) + ' ' + str(e.get('id', '')))]
        new_by_id = {e.get('id'): e for e in new}
        disabled = any(e.get('id') not in new_by_id or (
            (new_by_id[e.get('id')].get('states') or {}).get('enabled') is False or
            new_by_id[e.get('id')].get('disabled') is True) and
            (e.get('states') or {}).get('enabled') is not False for e in buttons)
        def busy(e):
            return (e.get('states') or {}).get('busy') is True or (e.get('role') == 'button' and re.search(r'(?i)\b(detener|investigando|stop)\b', e.get('name', '')) is not None)
        busy_seen = any(busy(e) for e in new) and not any(busy(e) for e in old)
        def containers(elements):
            values = {}
            for e in elements:
                c = e.get('container') or {}
                ident = str(c.get('id') or '')
                if ident and (re.search(r'(?i)answer|response|assistant', ident) or
                              c.get('role') in ('answer', 'response', 'assistant')):
                    values[ident] = tuple(c.get(k) for k in ('ordinal', 'revision'))
            return values
        old_containers, new_containers = containers(old), containers(new)
        container_delta = bool(new_containers.keys() - old_containers.keys()) or any(
            type(v) is int and type(old_containers.get(ident, (None, None))[i]) is int and
            v > old_containers[ident][i] for ident, revisions in new_containers.items() for i, v in enumerate(revisions))
        advanced = container_delta or any(type(after.get(k)) is int and type(before.get(k)) is int and after[k] > before[k]
                       for k in ('answer_count', 'container_count', 'answer_revision'))
        return result('submit_seen', cleared or disabled or busy_seen or advanced)
    if kind == 'focus_acquired':
        anchor = expectation.get('anchor') or action.get('element_id')
        focused = next((e for e in new if e.get('id') == anchor), None)
        return result(kind, anchor is not None and (after.get('focus') == anchor or
                      after.get('lane', {}).get('focus') == anchor or bool(focused and (focused.get('focused') or (focused.get('states') or {}).get('focused')))))
    if kind == 'scroll_moved':
        x, y = before.get('scroll'), after.get('scroll')
        return result(kind, type(x) is int and type(y) is int and 0 < abs(y - x) <= 120)
    return None


# @anchor:wa.learn.cu_memory.NoProgressTracker
class NoProgressTracker:
    def __init__(self, no_progress_steps=2):
        self.no_progress_steps = no_progress_steps
        self.no_progress_streak = self.stagnant_steps = self.consecutive_failures = 0
        self.visited_state_hashes = deque(maxlen=MAX_VISITED_HASHES)
        self.action_state_counts = OrderedDict()
        self.refused = OrderedDict()
        self.escalations = []

    # @anchor:wa.learn.cu_memory.NoProgressTracker.can_execute
    def can_execute(self, action, pre_state_hash):
        count = self.action_state_counts.get((canonical_action_sig(action), pre_state_hash), 0)
        return (False, 'repeated_action_target_state_pair') if count >= 2 else (True, None)

    # @anchor:wa.learn.cu_memory.NoProgressTracker.record_step
    def record_step(self, action, pre_state_hash, post_state_hash, *, outcome,
                    refusal_code=None, distance_before=None, distance_after=None):
        key = (canonical_action_sig(action), pre_state_hash)
        self.action_state_counts[key] = self.action_state_counts.pop(key, 0) + 1
        while len(self.action_state_counts) > MAX_STEPS_MEMORY:
            self.action_state_counts.popitem(last=False)
        self.visited_state_hashes.append(post_state_hash[:12])
        self.consecutive_failures = self.consecutive_failures + 1 if outcome == 'wrong_target_noop' else 0
        self.stagnant_steps = self.stagnant_steps + 1 if outcome == 'stagnant_verified' else 0
        progress = distance_before is not None and distance_after is not None and distance_after < distance_before
        self.no_progress_streak = self.no_progress_streak + 1 if outcome in ('wrong_target_noop', 'stagnant_verified') and not progress else 0
        repeated = False
        if refusal_code:
            key = (canonical_action_sig(action), refusal_code)
            self.refused[key] = self.refused.pop(key, 0) + 1
            repeated = self.refused[key] >= 2
            while len(self.refused) > MAX_REFUSED:
                self.refused.popitem(last=False)
        score = max(self.stagnant_steps + int(self.consecutive_failures > 0), 2 * int(repeated))
        reason = ('repeated_refusal' if repeated else 'stagnant_revisit' if self.stagnant_steps >= self.no_progress_steps
                  else 'two_consecutive_no_progress' if self.no_progress_streak >= 2 else None)
        row = dict(loop_score=score, should_escalate=reason is not None, escalate_reason=reason)
        if reason:
            self.escalations.append(row)
        return row


MAX_STEPS_MEMORY = 64


# @anchor:wa.learn.cu_memory.BlockedTargetFilter
class BlockedTargetFilter:
    _BLOCK_RE = re.compile(r'\b(?:upgrade|mejorar|plan|billing|account|cuenta|settings|ajustes|configuraci[oó]n|logout|log\s+out|cerrar\s+sesi[oó]n)\b', re.I)

    def __init__(self):
        self.blocked_control_present = 0
        self.by_reason = {}

    # @anchor:wa.learn.cu_memory.BlockedTargetFilter.check
    def check(self, element_or_name):
        name = element_or_name.get('name', '') if isinstance(element_or_name, dict) else element_or_name
        if self._BLOCK_RE.search(name):
            self.blocked_control_present += 1
            self.by_reason['blocked_control'] = self.by_reason.get('blocked_control', 0) + 1
            return False, 'blocked_control'
        return True, None

    # @anchor:wa.learn.cu_memory.BlockedTargetFilter.counts
    def counts(self):
        return dict(blocked_control_present=self.blocked_control_present, by_reason=dict(self.by_reason))
