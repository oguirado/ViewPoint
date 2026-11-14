# @module {"id":"wa.perceive.srf_identity","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.disambig"],"book":"HB-WA-s1-perceive"}
"""Identity surface boundary for the final os-cu-0 CLI.

AX is RAM-only. Only ax_options output may cross the model boundary. Inject a
transport(operation, payload) for fakes or a revised producer protocol.
"""
from __future__ import annotations

from functools import lru_cache
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import os
import shutil
import subprocess
import unicodedata
from typing import Protocol

# SRF_IDENTITY_BINARY overrides; otherwise the OpenSuperficies build output (build/bin) must be on PATH.
BINARY = os.environ.get('SRF_IDENTITY_BINARY') or shutil.which('srf-identity-surface') or 'srf-identity-surface'
IDENTITY_ENV = os.environ.get('SRF_IDENTITY_ENV', 'identity.env')
# Operator owns start/stop. Every adapter operation addresses an existing capsule.
CLI_MAPPING = {
    'frame': ('frame', '--container', '{capsule}', '--window', '{window}'),
    'ax': ('ax', '--container', '{capsule}', '--window', '{window}'),
    'text': ('ax', '--container', '{capsule}', '--window', '{window}', '--text-container', '{text_container}'),
    'click': ('input', '--container', '{capsule}', '--window', '{window}', '--click', '{point}'),
    'type': ('input', '--container', '{capsule}', '--window', '{window}', '--type', '{text}'),
    'key': ('input', '--container', '{capsule}', '--window', '{window}', '--key', '{keysym}'),
    # Wheel and press-move-release gestures (OpenSuperficies input --scroll / --drag, shipped by os-cu-0).
    'scroll': ('input', '--container', '{capsule}', '--window', '{window}', '--scroll', '{point}', '--dir', '{dir}'),
    'drag': ('input', '--container', '{capsule}', '--window', '{window}', '--drag', '{span}'),
}


# @anchor:wa.perceive.srf_identity.SurfaceError
class SurfaceError(RuntimeError):
    """Sanitized producer failure; stdout/stderr can contain private data."""
    def __init__(self, code, operation, detail=''):
        self.code, self.operation, self.detail = code, operation, detail
        super().__init__(f'{operation}: {code}')


# @anchor:wa.perceive.srf_identity.SurfaceNotReady
class SurfaceNotReady(SurfaceError):
    """Capsule or window is absent/not ready (producer exit 4)."""


# @anchor:wa.perceive.srf_identity.SurfaceRefused
class SurfaceRefused(SurfaceError):
    """Input refused or focus mismatch; never replay automatically."""


# @anchor:wa.perceive.srf_identity._producer_detail
def _producer_detail(stdout):
    """Bounded producer error detail ({"error":{"code","detail"}}); never page content."""
    try:
        detail = json.loads(stdout.splitlines()[0])['error']['detail']
        return detail[:160] if isinstance(detail, str) else ''
    except (ValueError, KeyError, IndexError, TypeError):
        return ''


# @anchor:wa.perceive.srf_identity.load_identity_env
def load_identity_env(path):
    """Parse only the producer allowlist; no shell evaluation or interpolation."""
    values = {}
    with Path(path).open(encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if line.startswith('export '):   # identity.env is shared with shells: accept `export KEY=value`
                line = line[len('export '):].lstrip()
            key, separator, value = line.partition('=')
            if separator and key in ('SRF_IDENTITY_STORE_ROOT', 'SRF_PROXY_REGISTRY'):
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if value:
                    values[key] = value
    if len(values) != 2:
        raise SurfaceError('config', 'environment')
    return values


_BLOCKED = re.compile(r'\b(?:upgrade|mejorar|plan|billing|facturaci[oó]n|try pro|subscribe|suscribirse|account|cuenta|settings|ajustes|configuraci[oó]n|logout|log out|cerrar sesi[oó]n)\b', re.I)
_NEW = re.compile(r'^(nuevo|new|new thread|new chat)$', re.I)
_SEND_BUTTON = re.compile(r'\b(?:send|enviar|submit|ask|preguntar)\b', re.I)
_LIVE_COUNTER = re.compile(r'\b\d+\s*(?:s|sec|seg|ms|m|min)?\b', re.I)
_STATE_KEYS = ('enabled', 'focused', 'expanded', 'checked', 'selected')
_STABLE_STATE_KEYS = ('busy', 'checked', 'current', 'disabled', 'expanded', 'hidden', 'pressed', 'selected')


# @anchor:wa.perceive.srf_identity.IdentitySurface
class IdentitySurface(Protocol):
    def start(self): ...
    def frame(self): ...
    def ax(self): ...
    def text(self, container): ...
    def input(self, action): ...
    def stop(self): ...


def _eligible(element):
    box = element.get('box')
    return (element.get('actionable') is True and element.get('visible', True) is True
            and _states(element).get('enabled') is not False
            and _states(element).get('hidden') is not True
            and _states(element).get('disabled') is not True
            and element.get('disabled') is not True and element.get('hidden') is not True
            and isinstance(box, list) and len(box) == 4
            and all(type(x) in (int, float) and math.isfinite(x) for x in box)
            and box[2] > 0 and box[3] > 0 and box[0] + box[2] > 0 and box[1] + box[3] > 0)


# @anchor:wa.perceive.srf_identity.ax_options
def ax_options(ax, *, rich_options=False):
    """Bounded text projection; navigation history is a non-actionable aggregate.

    Opaque generated ids also prevent producer ids leaking history titles. Never
    persist ax or the resolution map. The aggregate cannot dispatch a click.
    """
    rows, targets, navigation = [], {}, False
    for element in ax['elements']:
        element.pop('value', None)
        if not _eligible(element):
            continue
        name = element.get('name', '')
        region = element.get('region', '')
        if region == 'navigation' and not _NEW.fullmatch(name):
            navigation = True
            continue
        if _BLOCKED.search(name):
            continue
        rows.append(element)
    rows.sort(key=lambda e: e.get('region') != 'main')
    result = []
    for element in rows[:59 if navigation else 60]:
        option_id = 'e' + str(len(result))
        result.append({'id': option_id, 'role': element.get('role', ''),
                       'name': str(element.get('name', ''))[:120],
                       'region': element.get('region', ''),
                       'container_ordinal': (element.get('container') or {}).get('ordinal'),
                       'value_len': _valid_value_len(element),
                       'focused': element.get('focused') if type(element.get('focused')) is bool else None,
                       'states': {k: v if type(v := _states(element).get(k)) is bool else None
                                  for k in _STATE_KEYS}})
        targets[option_id] = element
    if navigation:
        result.append({'id': 'navigation', 'role': 'group', 'name': 'Navigation/history',
                       'region': 'navigation', 'container_ordinal': None})
    if rich_options:
        from wender.perceive.disambig import disambiguate, format_label
        sources = [dict(targets[option['id']], name=option['name'], option_id=option['id'],
                        _source=targets[option['id']], _elements=ax['elements'])
                   for option in result if option['id'] != 'navigation']
        details = iter(disambiguate(sources))
        for option in result:
            if option['id'] == 'navigation':
                option.update(disambig=None, label='Navigation/history')
            else:
                detail = next(details)
                option.update(disambig=detail, label=format_label(option, detail))
    return result, targets


# @anchor:wa.perceive.srf_identity._generating
def _generating(ax):
    return any((e.get('states') or {}).get('busy') is True or
               (e.get('role') == 'button' and e.get('actionable') is True and re.search(
                   r'^(?:Stop(?: response)?|Detener|Investigando)\b', e.get('name', ''), re.I))
               for e in ax.get('elements', []))


def _plain_answer_control(row, rows):
    # Follow producer ancestry; a nested code/snippet Copy is never a terminal.
    seen = set()
    while row:
        container = row.get('container') or {}
        if (row.get('role') in ('code', 'pre') or
                any(value in ('code', 'code_block', 'user_turn') for value in
                    (row.get('scope'), container.get('scope'), container.get('role')))):
            return False
        parent = row.get('parent', row.get('parent_id'))
        if isinstance(parent, dict):
            parent = parent.get('id')
        if type(parent) is int:
            parent = rows[parent].get('id') if 0 <= parent < len(rows) else '__absent__'
        if parent is None:
            return True
        if parent in seen:
            return False
        seen.add(parent)
        matches = [r for r in rows if r.get('id') == parent]
        if len(matches) != 1:
            return False
        row = matches[0]
    return False


def _terminal_bar(ax, cid, *, english=True):
    elements = ax.get('elements', [])
    bars = [(i, e) for i, e in enumerate(elements) if e.get('role') == 'button'
            and e.get('region') == 'main' and (e.get('container') or {}).get('id') == cid
            and _plain_answer_control(e, elements)]
    copies = [(i, e) for i, e in bars if re.fullmatch(r'Copy' if english else r'Copiar|Copy', e.get('name', ''), re.I)]
    if len(copies) != 1:
        return None
    if not english:
        return copies[0][0]
    helpful = [(i, e) for i, e in bars if e.get('name', '').casefold() == 'helpful']
    if len(helpful) != 1:
        return None
    boxes = [e.get('box') for _, e in (copies[0], helpful[0])]
    if not all(isinstance(b, list) and len(b) == 4 and
               all(type(v) in (int, float) and math.isfinite(v) for v in b) and
               b[2] > 0 and b[3] > 0 for b in boxes):
        return None
    return min(copies[0][0], helpful[0][0]) if abs(boxes[0][1] - boxes[1][1]) <= 4 else None


# @anchor:wa.perceive.srf_identity._viewport_thread_margin
def _viewport_thread_margin(ax, target, frame):
    # The producer exposes text leaves, not a panel node. Ground the blank
    # margin in the current thread viewport and reject every intersecting control.
    url = (ax.get('window') or {}).get('url', ax.get('url'))
    box = target.get('box')
    if (frame is None or ax.get('truncated') is not False or _generating(ax) or
            not isinstance(url, str) or not re.fullmatch(r'https://(?:www\.)?perplexity\.ai/search/[^/?#]+(?:[?#].*)?', url) or
            target.get('role') != 'StaticText' or target.get('region') != 'main' or
            target.get('actionable') is True or target.get('visible') is False or
            not isinstance(box, list) or len(box) != 4 or
            not all(type(v) in (int, float) and math.isfinite(v) for v in box)):
        raise SurfaceRefused('thread_focus_evidence_unavailable', 'click')
    composers = [e for e in ax['elements'] if e.get('role') == 'textbox'
                 and e.get('region') == 'main' and _eligible(e)]
    x = 6   # the producer CLI takes integer pixels; a float y gave rc 2 usage live [MEASURED]
    y = (143 + min(frame['height'], min((e['box'][1] for e in composers), default=143))) // 2
    if (not composers or not 0 < x < frame['width'] or
            not 143 < y < min(frame['height'], min(e['box'][1] for e in composers))):
        raise SurfaceRefused('thread_focus_evidence_unavailable', 'click')
    for row in ax['elements']:
        if row.get('actionable') is not True and row.get('role') != 'link':
            continue
        bounds = row.get('box')
        if not (isinstance(bounds, list) and len(bounds) == 4 and
                all(type(v) in (int, float) and math.isfinite(v) for v in bounds)):
            raise SurfaceRefused('thread_focus_evidence_unavailable', 'click')
        if bounds[0] <= x <= bounds[0] + bounds[2] and bounds[1] <= y <= bounds[1] + bounds[3]:
            raise SurfaceRefused('thread_focus_obstructed', 'click')
    return x, y


def _thread_margin(ax, target, frame):
    # Require an observed panel, never infer its viewport from a text leaf.
    if (ax.get('truncated') is not False or target.get('role') not in ('article', 'document')
            or target.get('region') != 'main' or not (target.get('container') or {}).get('id')
            or target.get('visible') is False or _states(target).get('hidden') is True
            or _generating(ax)):
        raise SurfaceRefused('thread_focus_evidence_unavailable', 'click')
    box = target.get('box')
    if not (isinstance(box, list) and len(box) == 4 and
            all(type(v) in (int, float) and math.isfinite(v) for v in box) and
            box[0] >= 0 and box[1] >= 0 and box[2] > 0 and box[3] > 0 and
            box[0] + box[2] <= frame['width'] and box[1] + box[3] <= frame['height']):
        raise SurfaceRefused('thread_focus_evidence_unavailable', 'click')
    composers = [e for e in ax['elements'] if e.get('role') == 'textbox'
                 and e.get('region') == 'main' and _eligible(e)]
    if not composers:
        raise SurfaceRefused('thread_focus_evidence_unavailable', 'click')
    area = max(e['box'][2] * e['box'][3] for e in composers)
    composers = [e for e in composers if e['box'][2] * e['box'][3] == area]
    if len(composers) != 1:
        raise SurfaceRefused('thread_focus_evidence_unavailable', 'click')
    x = box[0] + 6
    y = min(box[1] + box[3], composers[0]['box'][1]) - 6
    if not (box[0] < x < box[0] + box[2] and box[1] < y < box[1] + box[3]
            and y < composers[0]['box'][1]):
        raise SurfaceRefused('thread_focus_evidence_unavailable', 'click')
    for row in ax['elements']:
        if row.get('actionable') is not True and row.get('role') != 'link':
            continue
        bounds = row.get('box')
        if not (isinstance(bounds, list) and len(bounds) == 4 and
                all(type(v) in (int, float) and math.isfinite(v) for v in bounds)):
            raise SurfaceRefused('thread_focus_evidence_unavailable', 'click')
        if bounds[0] <= x <= bounds[0] + bounds[2] and bounds[1] <= y <= bounds[1] + bounds[3]:
            raise SurfaceRefused('thread_focus_obstructed', 'click')
    return x, y


# @anchor:wa.perceive.srf_identity.last_answer_container
def last_answer_container(ax, *, ownership=None):
    """Select the latest answer; preserve legacy stateless Spanish lookup."""
    elements = ax.get('elements', [])
    if not isinstance(elements, list) or ax.get('truncated') is True:
        return None
    markers = [(i, e) for i, e in enumerate(elements)
               if e.get('role') == 'button' and e.get('region') == 'main'
               and re.match(r'^(?:Investigado|Finished)\b', e.get('name', ''), re.I)]
    if not markers:
        return None
    def ordinal(pair):
        value = (pair[1].get('container') or {}).get('ordinal')
        return value if type(value) is int else -1
    latest = max(ordinal(pair) for pair in markers)
    candidates = [pair for pair in markers if ordinal(pair) == latest]
    # Spanish fixtures retain their original last-in-traversal tie ordering.
    if candidates and all(re.match(r'^Investigado\b', e.get('name', ''), re.I) for _, e in candidates):
        candidates = [max(candidates, key=lambda pair: pair[0])]
    if len(candidates) != 1:
        return None
    index, marker = candidates[0]
    cid = (marker.get('container') or {}).get('id')
    if not isinstance(cid, str) or not cid:
        return None
    if ownership is not None and (cid in ownership.boundary_containers or
            (ownership.boundary_max_ordinal is not None and latest <= ownership.boundary_max_ordinal)):
        return None
    # Legacy stateless Spanish callers may inspect the preceding answer while
    # a follow-up renders. Completion callers supply ownership and retain the
    # generating veto, as does every English selection.
    if _generating(ax) and (ownership is not None or not re.match(r'^Investigado\b', marker.get('name', ''), re.I)):
        return None
    if re.match(r'^Investigado\b', marker.get('name', ''), re.I):
        if ownership is not None:
            return answer_container_for_turn(ax, ownership)
        return cid if any(e.get('role') == 'button' and
                          re.fullmatch(r'Copiar|Copy', e.get('name', ''), re.I) and
                          e.get('region') == 'main' and
                          (e.get('container') or {}).get('id') == cid
                          for e in elements[index + 1:]) else None
    if ax.get('truncated') is not False or latest < 0:
        return None
    terminal = _terminal_bar(ax, cid)
    return cid if terminal is not None and terminal > index else None


# @anchor:wa.perceive.srf_identity.answer_container_for_turn
def answer_container_for_turn(ax, ownership=None):
    """Pure turn-aware selection entry point for learn callers."""
    from wender.perceive.disambig import answer_container_for_turn as select
    return select(ax, ownership)


def _states(element):
    states = element.get('states')
    return states if isinstance(states, dict) else {}


def _valid_value_len(element):
    value = element.get('value_len')
    return value if type(value) is int and value >= 0 else None


# @anchor:wa.perceive.srf_identity.canon_head
def canon_head(name):
    """Normalize a label to two words, excluding live timer counters."""
    if not isinstance(name, str) or not name:
        return ''
    norm = re.sub(r'\s+', ' ', unicodedata.normalize('NFC', name)).strip().casefold()
    norm = _LIVE_COUNTER.sub('<n>', norm)
    words = [w for w in re.split(r'[^a-z0-9_<>\u0080-\uffff]+', norm)
             if w and w != '<n>' and not w.isdigit()]
    return (' '.join(words[:2]) if words else norm[:24])[:24]


def _snapshot_key(ax):
    """RAM-only immutable key; excludes raw values and detects same-frame edits."""
    rows = []
    for index, element in enumerate(ax.get('elements') or []):
        if not isinstance(element, dict):
            continue
        states = _states(element)
        enabled = (False if states.get('enabled') is False or states.get('disabled') is True
                   or element.get('disabled') is True else True if states.get('enabled') is True else None)
        focused = (True if element.get('focused') is True or states.get('focused') is True
                   else False if element.get('focused') is False or states.get('focused') is False else None)
        pairs = tuple(sorted((k, v) for k, v in states.items()
                             if type(v) is bool and k != 'value'))
        box = element.get('box')
        spatial = ((int(box[1]) // 32, int(box[0]) // 64)
                   if isinstance(box, (list, tuple)) and len(box) == 4
                   and all(type(v) in (int, float) and math.isfinite(v) for v in box)
                   else (10**9, 10**9))
        rows.append((spatial, index,
                     str(element.get('id') or element.get('anchor_id') or f'idx:{index}'),
                     str(element.get('role') or ''), str(element.get('name') or ''),
                     element.get('actionable') is True, enabled, focused,
                     _valid_value_len(element), pairs))
    return tuple(rows)


@lru_cache(maxsize=1)
def _snapshot_hashes(frame_seq, snapshot):
    """Canonicalize once per observation, keeping only the latest frame in RAM.

    Include the immutable field key as well as frame_seq: reused sequence numbers
    and mutable synthetic snapshots must never produce stale screen identities.
    Tuple framing preserves the existing digest contract without delimiter aliases.
    """
    full_rows, layout_rows, state_rows = [], [], []
    for _, _, el_id, role, name, actionable, enabled, focused, vlen, pairs in sorted(snapshot):
        norm = re.sub(r'\s+', ' ', unicodedata.normalize('NFC', name)).strip().casefold()[:120]
        head = canon_head(name)
        full_rows.append((el_id, role, head, actionable, enabled, focused, vlen, pairs))
        layout_rows.append((role, head, actionable, enabled,
                            tuple((k, v) for k, v in pairs if k in _STABLE_STATE_KEYS)))
        state_rows.append((el_id, role, norm, actionable, enabled, focused, vlen, pairs))
    def digest(rows, length):
        payload = json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(payload).hexdigest()[:length]
    return digest(full_rows, 12), digest(layout_rows, 12), digest(state_rows, 16)


def _observation_hashes(ax):
    seq = ax.get('frame_seq')
    return _snapshot_hashes(seq if type(seq) is int else None, _snapshot_key(ax))


# @anchor:wa.perceive.srf_identity.srf_hash
def srf_hash(ax, *, include_state=True):
    """Screen sketch; disable state for a focus/typing invariant layout hash."""
    return _observation_hashes(ax)[0 if include_state else 1]


# @anchor:wa.perceive.srf_identity.srf_state_hash
def srf_state_hash(ax):
    """Digest complete label and validated state fields, never raw input values."""
    return _observation_hashes(ax)[2]


# @anchor:wa.perceive.srf_identity.screen_flags
def screen_flags(ax, rule=None):
    """Count blocked controls before projection; emit digests of private labels."""
    blocked_sha8, history_handles = [], []
    omitted_ineligible = sidebar_hidden = 0
    for element in (ax or {}).get('elements') or []:
        if not isinstance(element, dict):
            continue
        name = str(element.get('name') or '')
        role, region = str(element.get('role') or ''), str(element.get('region') or '')
        states = _states(element)
        enabled = (states.get('enabled') is not False and states.get('disabled') is not True
                   and element.get('disabled') is not True and element.get('enabled') is not False)
        actionable = element.get('actionable', True) is not False
        eligible = _eligible(element)
        omitted_ineligible += not eligible
        if region == 'navigation' and not _NEW.fullmatch(name):
            sidebar_hidden += 1
            digest = hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]
            history_handles.append(f'history[#{sidebar_hidden}, {digest}]')
            continue
        disabled_target = (isinstance(rule, dict)
                           and ('role' not in rule or role == rule['role'])
                           and ('region' not in rule or region == rule['region'])
                           and re.search(rule.get('pattern', '.*'), name, re.I) is not None
                           and (not enabled or not actionable or not eligible))
        disabled_send = (role in ('button', 'submit') and (not enabled or not actionable)
                         and (bool(_SEND_BUTTON.search(name)) or not enabled))
        if _BLOCKED.search(name) or disabled_target or disabled_send:
            blocked_sha8.append(hashlib.sha256(name.encode('utf-8')).hexdigest()[:8])
    full_hash, layout_hash, state_hash = _observation_hashes(ax)
    return {'srf_hash': full_hash, 'srf_layout_hash': layout_hash,
            'srf_state_hash': state_hash, 'blocked_control_present': len(blocked_sha8),
            'blocked_names_sha8': blocked_sha8, 'omitted_ineligible': omitted_ineligible,
            'sidebar_hidden': sidebar_hidden, 'history_handles': history_handles}


# @anchor:wa.perceive.srf_identity.blocked_control_present
def blocked_control_present(ax, rule=None):
    """Tripwire for blocked labels and disabled send or rule-target controls."""
    return screen_flags(ax, rule=rule)['blocked_control_present'] > 0


# @anchor:wa.perceive.srf_identity.SrfIdentitySurface
class SrfIdentitySurface:
    """Attach to an operator-owned capsule; never start or stop the live surface."""

    def __init__(self, identity='perplexity-1', *, container='srf-id-perplexity-1',
                 window='w1', identity_env=IDENTITY_ENV, transport=None, binary=BINARY,
                 mapping=None, ram_dir='/dev/shm', timeout=30, trace=None):
        if any(not isinstance(v, str) or not v or v.startswith('-')
               for v in (identity, container, window)):
            raise ValueError('invalid identity/container/window')
        ram = Path(ram_dir).resolve()
        if not ram.is_relative_to('/dev/shm') or not ram.is_dir():
            raise ValueError('frames require an existing RAM directory')
        self.identity, self.ram_dir = identity, str(ram)
        self.container, self.window, self.identity_env = container, window, identity_env
        self.binary, self.mapping = binary, mapping or CLI_MAPPING
        self.timeout, self.transport, self.trace = timeout, transport, trace
        self.started, self.last_frame = False, None
        self._latest_ax = None
        self.violations = []

    def _violation(self, code):
        row = {'event': 'alignment_violation', 'code': code}
        self.violations.append(row)
        if self.trace is not None:
            self.trace.write(json.dumps(row) + '\n')
            self.trace.flush()

    def _call(self, operation, **payload):
        if self.transport is not None:
            return self.transport(operation, payload)
        values = dict(capsule=self.container, window=self.window, **payload)
        argv = [self.binary, *(word.format_map(values) for word in self.mapping[operation])]
        if operation in ('click', 'drag') and 'button' in payload:
            argv.extend(['--button', str(payload['button'])])
        if operation == 'scroll' and payload.get('modifier') in ('ctrl', 'shift'):
            argv.extend(['--modifier', payload['modifier']])
        if operation == 'type' and payload.get('paste') is True:
            argv.append('--paste')   # clipboard + ctrl+v inside the capsule (OpenSuperficies 60e53e3): rich editors keep the text intact
        # The producer CLI shells out to docker/xdotool: it needs PATH. Only PATH is
        # inherited; every other variable comes from the identity env allowlist.
        env = dict(load_identity_env(self.identity_env), PATH=os.environ.get('PATH', '/usr/bin:/bin'))
        response = subprocess.run(argv, capture_output=True, timeout=self.timeout,
                                  check=False, env=env)
        if response.returncode:
            code = {2: 'usage', 3: 'config_proxy', 4: 'not_ready',
                    5: 'malformed_capture', 6: 'refused_focus'}.get(response.returncode, 'producer_failure')
            error = {4: SurfaceNotReady, 6: SurfaceRefused}.get(response.returncode, SurfaceError)
            raise error(code, operation, _producer_detail(response.stdout))
        try:
            lines = response.stdout.splitlines()
            if len(lines) != 1:
                raise ValueError('expected one JSON line')
            result = json.loads(lines[0])
            if not isinstance(result, dict):
                raise ValueError('expected object')
            return result
        except (ValueError, TypeError):
            raise SurfaceError('malformed_capture', operation) from None

    # @anchor:wa.perceive.srf_identity.SrfIdentitySurface.start
    def start(self):
        if self.started:
            raise RuntimeError('surface already attached')
        self.started = True

    # @anchor:wa.perceive.srf_identity.SrfIdentitySurface.frame
    def frame(self):
        self.last_frame = None
        self._latest_ax = None
        frame = self._call('frame')
        if (not isinstance(frame, dict) or not isinstance(frame.get('path'), str)
                or not Path(frame['path']).resolve().is_relative_to('/dev/shm')
                or type(frame.get('seq')) is not int
                or any(type(frame.get(k)) is not int or frame[k] <= 0 for k in ('width', 'height'))
                or not isinstance(frame.get('window'), dict)
                or frame['window'].get('id') != self.window
                or frame['window'].get('w') != frame['width']
                or frame['window'].get('h') != frame['height']):
            raise SurfaceError('malformed_capture', 'frame')
        self.last_frame = frame
        return frame

    def _aligned_ax(self, operation='ax', *, refetch=True, **payload):
        if self.last_frame is None:
            self.frame()
        for attempt in range(2):
            ax = self._call(operation, **payload)
            if not isinstance(ax, dict) or not isinstance(ax.get('elements'), list):
                raise SurfaceError('malformed_capture', 'ax')
            if type(ax.get('frame_seq')) is int and ax['frame_seq'] == self.last_frame['seq']:
                break
            self._violation('frame_seq_mismatch')
            if attempt or not refetch:
                raise SurfaceError('alignment', 'ax')
            self.frame()
        if (not isinstance(ax.get('window'), dict)
                or any(ax['window'].get(k) != self.last_frame['window'].get(k)
                       for k in ('id', 'w', 'h'))):
            self._violation('window_mismatch')
            raise SurfaceError('alignment', 'ax')
        for element in ax['elements']:
            if element.get('actionable') is True:
                box = element.get('box')
                if (not isinstance(box, list) or len(box) != 4
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
                        or box[0] < 0 or box[1] < 0 or box[2] <= 0 or box[3] <= 0
                        or box[0] + box[2] > self.last_frame['width']
                        or box[1] + box[3] > self.last_frame['height']):
                    self._violation('actionable_box_outside_window')
                    raise SurfaceError('alignment', 'ax')
        return ax

    # @anchor:wa.perceive.srf_identity.SrfIdentitySurface.ax
    def ax(self, *, refetch=True):
        self._latest_ax = None
        ax = self._aligned_ax(refetch=refetch)
        self._latest_ax = copy.deepcopy(ax)
        return ax

    # @anchor:wa.perceive.srf_identity.SrfIdentitySurface.text
    def text(self, container):
        if not isinstance(container, str) or re.fullmatch(r'[0-9a-fA-F]+', container) is None:
            raise ValueError('one hex container id required')
        value = self._aligned_ax('text', text_container=container).get('text')
        if (not isinstance(value, dict) or value.get('container_id') != container
                or not isinstance(value.get('text'), str)
                or type(value.get('bytes')) is not int
                or value['bytes'] != len(value['text'].encode('utf-8'))
                or value['bytes'] > 65536):
            raise SurfaceError('malformed_capture', 'text')
        return value['text']

    # @anchor:wa.perceive.srf_identity.SrfIdentitySurface.input
    def input(self, action):
        kind = action['kind']
        if kind == 'click':
            supplied = action['element']
            rows = (self._latest_ax or {}).get('elements', [])
            matches = [e for e in rows if e.get('id') and e.get('id') == supplied.get('id')]
            if len(matches) != 1 or {k: v for k, v in matches[0].items() if k != 'value'} != {k: v for k, v in supplied.items() if k != 'value'}:
                raise ValueError('target custody')
            target = matches[0]
            if action.get('purpose') == 'viewport_thread_focus':
                x, y = _viewport_thread_margin(self._latest_ax, target, self.last_frame)
                result = self._call('click', point=f'{x:g},{y:g}')
                return self._validated_input(result, kind)
            if action.get('purpose') == 'thread_focus':
                x, y = _thread_margin(self._latest_ax, target, self.last_frame)
                result = self._call('click', point=f'{x:g},{y:g}')
                return self._validated_input(result, kind)
            if re.search(r'^(?:Stop|Detener)\b', target.get('name', ''), re.I):
                raise SurfaceRefused('generation_control', 'click')
            _, targets = ax_options({'elements': [target]})
            if not targets or self.last_frame is None:
                raise ValueError('blocked, invisible or ungrounded target')
            x, y, w, h = target['box']
            if x < 0 or y < 0 or x + w > self.last_frame['width'] or y + h > self.last_frame['height']:
                self._violation('actionable_box_outside_window')
                raise SurfaceError('alignment', 'click')
            result = self._call('click', point=f'{x + w / 2:g},{y + h / 2:g}',
                                **({'button': action['button']} if 'button' in action else {}))
        elif kind == 'type':
            if not isinstance(action.get('text'), str) or not action['text'].isascii():
                raise ValueError('ASCII required')
            result = self._call('type', text=action['text'], **({'paste': True} if getattr(self, 'paste_text', False) else {}))
        elif kind == 'key':
            if action['keysym'].casefold() in ('esc', 'escape') and (self._latest_ax is None or _generating(self._latest_ax)):
                raise SurfaceRefused('generation_key', 'key')
            result = self._call('key', keysym=action['keysym'])
        else:
            raise ValueError('unsupported identity input')
        return self._validated_input(result, kind)

    def _validated_input(self, result, kind):
        if not isinstance(result, dict) or result.get('focus_verified') is not True:
            raise SurfaceRefused('refused_focus', kind)
        if type(result.get('input_mono_ns')) is not int:
            raise SurfaceError('malformed_capture', kind)
        return result

    # @anchor:wa.perceive.srf_identity.SrfIdentitySurface.stop
    def stop(self):
        self.started = False
        self.last_frame = None
        self._latest_ax = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


# @anchor:wa.perceive.srf_identity.GoalIdentitySurface
class GoalIdentitySurface(SrfIdentitySurface):
    """Episode-local target custody; legacy mappings and filters stay frozen."""

    # @anchor:wa.perceive.srf_identity.GoalIdentitySurface.install_goal_session
    def install_goal_session(self, *, owner, policy, deadline, clock):
        if getattr(self, '_goal_session', None) is not None or not owner.check_ready():
            raise SurfaceRefused('goal_owner', 'setup')
        self._goal_session = (owner, policy, deadline, clock)
        self._goal_targets = None
        self._goal_focus = None

    # @anchor:wa.perceive.srf_identity.GoalIdentitySurface.publish_goal_targets
    def publish_goal_targets(self, *, frame_seq, generation, frame_digest, elements, marks):
        self._goal_targets = None
        frame = self.last_frame or {}
        if (frame_seq, generation, frame_digest) != tuple(frame.get(k) for k in
                ('seq', 'generation', 'sha256_rgb24')):
            raise SurfaceRefused('goal_stale', 'publish')
        if not isinstance(elements, dict) or not isinstance(marks, dict):
            raise SurfaceRefused('goal_grounding', 'publish')
        raw = (self._latest_ax or {}).get('elements', [])
        if any(sum(e == row for row in raw) != 1 for e in elements.values()):
            raise SurfaceRefused('goal_grounding', 'publish')
        identity = (frame['generation'], json.dumps(frame['window'], sort_keys=True))
        if self._goal_focus != identity:
            self._goal_focus = None
        if any(_states(e).get('focused') is True for e in raw):
            self._goal_focus = None
        self._goal_targets = (copy.deepcopy(frame), copy.deepcopy(elements), copy.deepcopy(marks))

    # @anchor:wa.perceive.srf_identity.GoalIdentitySurface.prepare_goal_input
    def prepare_goal_input(self, action):
        session = getattr(self, '_goal_session', None)
        if session is None or not session[0].check_ready():
            raise SurfaceRefused('goal_owner', 'input')
        owner, policy, deadline, clock = session
        if clock() >= deadline:
            raise SurfaceRefused('goal_deadline', 'input')
        custody = self._goal_targets
        if custody is None or custody[0] != self.last_frame:
            raise SurfaceRefused('goal_stale', 'input')
        frame, elements, marks = custody
        kind = action.get('kind')
        allowed = {'click': {'kind', 'element_id', 'mark_id', 'x', 'y'},
                   'type': {'kind', 'text'}, 'key': {'kind', 'keysym'}}
        if kind not in allowed or action.keys() - allowed[kind]:
            raise SurfaceRefused('goal_fields', 'input')
        if kind == 'click':
            if sum(k in action for k in ('element_id', 'mark_id')) != 1:
                raise SurfaceRefused('goal_grounding', 'input')
            key = 'element_id' if 'element_id' in action else 'mark_id'
            target = (elements if key == 'element_id' else marks).get(action[key])
            if target is None:
                raise SurfaceRefused('goal_grounding', 'input')
            if key == 'element_id':
                if sum(target == row for row in (self._latest_ax or {}).get('elements', [])) != 1:
                    raise SurfaceRefused('goal_stale', 'input')
                policy.check_target(target)
                # Frozen navigation and blocked-control filters still apply.
                if not ax_options({'elements': [copy.deepcopy(target)]})[1]:
                    raise SurfaceRefused('goal_upstream_filter', 'input')
                x, y, w, h = target['box']
            else:
                x, y, w, h = (target[k] for k in ('x', 'y', 'w', 'h'))
            if (not all(type(v) is int for v in (x, y, w, h))
                    or min(x, y) < 0 or min(w, h) <= 0
                    or x + w > frame['width'] or y + h > frame['height']):
                raise SurfaceRefused('goal_grounding', 'input')
            cx, cy = x + w // 2, y + h // 2
            if ('x' in action or 'y' in action) and (action.get('x'), action.get('y')) != (cx, cy):
                raise SurfaceRefused('goal_center', 'input')
            if key == 'mark_id':
                for row in (self._latest_ax or {}).get('elements', []):
                    box = row.get('box')
                    if policy.covers_mark(row) and isinstance(box, list) and len(box) == 4 and box[0] <= cx < box[0]+box[2] and box[1] <= cy < box[1]+box[3]:
                        policy.check_target(row)
                        raise SurfaceRefused('goal_ax_preferred', 'input')
            return ('click', {'point': f'{cx:g},{cy:g}'})
        raw = (self._latest_ax or {}).get('elements', [])
        focused = [e for e in raw if _states(e).get('focused') is True]
        if focused:
            if len(focused) != 1:
                raise SurfaceRefused('goal_focus', 'input')
            policy.check_target(focused[0])
            if focused[0].get('role', '').casefold() not in ('textbox', 'entry', 'textarea', 'searchbox', 'combobox'):
                raise SurfaceRefused('goal_focus', 'input')
        elif self._goal_focus != (frame['generation'], json.dumps(frame['window'], sort_keys=True)):
            raise SurfaceRefused('goal_focus', 'input')
        if kind == 'type':
            if not isinstance(action.get('text'), str) or not action['text'].isascii():
                raise SurfaceRefused('goal_ascii', 'input')
            return ('type', {'text': action['text']})
        key = action.get('keysym')
        if key not in ('Left', 'Right', 'Up', 'Down', 'Home', 'End', 'BackSpace', 'Delete', 'Return', 'space', 'Escape', 'Esc'):
            raise SurfaceRefused('goal_key', 'input')
        generation_known = any(type(_states(e).get('busy')) is bool for e in raw)
        if key in ('Escape', 'Esc') and (not generation_known or self._latest_ax is None or _generating(self._latest_ax)):
            raise SurfaceRefused('generation_key', 'input')
        return ('key', {'keysym': key})

    # @anchor:wa.perceive.srf_identity.GoalIdentitySurface.input
    def input(self, action):
        kind, payload = self.prepare_goal_input(action)
        try:
            result = self._validated_input(self._call(kind, **payload), kind)
            if result['input_mono_ns'] < 0:
                raise SurfaceError('malformed_capture', kind)
            if action.get('mark_id') is not None:
                self._goal_focus = (self.last_frame['generation'], json.dumps(self.last_frame['window'], sort_keys=True))
            return result
        except BaseException:
            self._goal_focus = None
            self._goal_targets = None
            self._goal_session[0].uncertain(action)
            raise

    # @anchor:wa.perceive.srf_identity.GoalIdentitySurface.stop
    def stop(self):
        self._goal_targets = self._goal_focus = self._goal_session = None
        super().stop()
