# @module {"id":"wa.perceive.disambig","layer":1,"owner":"PERCEIVE","deps":[],"book":"HB-WA-s1-perceive"}
"""Pure W3 option disambiguation and turn ownership.

Contracts: FINAL_ENGINE_TAGGING_ARCHITECTURE sections 3.3 and 5.2, bd_w3a,
bd_w3b. All ownership state is caller-owned, RAM-only and text-free. Context
never uses history links, input values, anchor aliases, or assistant prose.
"""
from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata

_CONTEXT_ROLES = {'heading', 'row', 'main', 'form', 'dialog', 'toolbar', 'menu', 'search'}
_COMPOSITES = {'row', 'group', 'article', 'listitem'}
_TERMINAL = re.compile(r'Copiar|Copy|Bifurcar|No [uú]til|\d+\s+fuentes', re.I)


def _norm(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', text or '')).strip().casefold()


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# @anchor:wa.perceive.disambig.canon_name
def canon_name(raw_name: str | None) -> tuple[str, bool]:
    name = _norm(raw_name)
    for stem, busy in (('investigando', True), ('investigado', False), ('generating', True)):
        if re.match(r'^' + stem + r'\s+\d+\s*s\b', name):
            return stem + ' <n>', busy
    if re.match(r'^(detener(?:\s+la\s+respuesta)?|stop(?:\s+generating|\s+response)?)\b', name):
        return ('detener' if name.startswith('detener') else 'stop'), True
    return name, False


def _container(row):
    return row.get('container') or {}


def _box(row):
    box = row.get('box')
    if (isinstance(box, (list, tuple)) and len(box) == 4
            and all(type(v) is int for v in box) and box[2] > 0 and box[3] > 0):
        return box
    return None


def _same_landmark(a, b):
    return (a.get('region') == b.get('region')
            and _container(a).get('id') == _container(b).get('id')
            and a.get('landmark_id') == b.get('landmark_id'))


# @anchor:wa.perceive.disambig.container_vs_leaf
def container_vs_leaf(elements: list[dict]) -> dict[int, list[int]]:
    """Spec 1.1: dot-prefix OR integer R10 containment, within one landmark."""
    found = {}
    for i, parent in enumerate(elements):
        if parent.get('role') not in _COMPOSITES:
            continue
        prefix = parent.get('anchor_id') or parent.get('id')
        p = _box(parent)
        for j, child in enumerate(elements):
            if i == j or child.get('actionable') is not True or child.get('role') != 'button':
                continue
            if not _same_landmark(parent, child):
                continue
            anchor = child.get('anchor_id') or child.get('id')
            anchored = bool(isinstance(prefix, str) and isinstance(anchor, str)
                            and anchor.startswith(prefix + '.'))
            c = _box(child)
            contained = False
            if p and c:
                inter = max(0, min(p[0]+p[2], c[0]+c[2])-max(p[0], c[0])) * max(
                    0, min(p[1]+p[3], c[1]+c[3])-max(p[1], c[1]))
                contained = p[2]*p[3] >= 2*c[2]*c[3] and 1000*inter >= 850*c[2]*c[3]
            if anchored or contained:
                found.setdefault(i, []).append(j)
    return found


def _lookup(elements):
    return {e['id']: i for i, e in enumerate(elements) if isinstance(e.get('id'), str)}


def _parent_index(row, lookup, count):
    parent = row.get('parent', row.get('parent_id'))
    if isinstance(parent, dict):
        parent = parent.get('id')
    if type(parent) is int:
        return parent if 0 <= parent < count else None
    return lookup.get(parent) if isinstance(parent, str) else None


def _safe_title(row):
    if row.get('role') not in _CONTEXT_ROLES or row.get('region') in ('navigation', 'sidebar'):
        return None
    name = re.sub(r'\s+', ' ', unicodedata.normalize('NFC', row.get('name') or '')).strip()
    if re.search(r'(?i)(?:https?://|www\.|@)', name):
        return None
    return name[:32] or None


def _context(index, elements, lookup, prompt_landmark_id=None):
    row = elements[index]
    region = row.get('region') or 'unknown'
    ordinal = _container(row).get('ordinal')
    fallback = f'{region}#{ordinal}' if type(ordinal) is int else region
    if region in ('navigation', 'sidebar'):
        return fallback[:32]
    if prompt_landmark_id is not None and row.get('landmark_id', _container(row).get('id')) != prompt_landmark_id:
        return None
    seen = {index}
    parent = _parent_index(row, lookup, len(elements))
    for _ in range(40):
        if parent is None or parent in seen:
            break
        seen.add(parent)
        candidate = elements[parent]
        if (candidate.get('region') != region
                or (candidate.get('landmark_id') is not None
                    and candidate.get('landmark_id') != row.get('landmark_id'))):
            break
        title = _safe_title(candidate)
        if title:
            return title
        parent = _parent_index(candidate, lookup, len(elements))
    b = _box(row)
    for i, candidate in enumerate(elements):
        if i == index or not _same_landmark(row, candidate):
            continue
        c = _box(candidate)
        # A main control may sit at x<240 in synthetic fixtures; never borrow
        # a sidebar-band sibling, even if its producer region was mislabeled.
        if b and c and c[0] >= 240 and abs((2*b[1]+b[3])-(2*c[1]+c[3])) <= 2*max(18, b[3]):
            title = _safe_title(candidate)
            if title:
                return title
    return fallback[:32]


# @anchor:wa.perceive.disambig.container_ctx_for
def container_ctx_for(index: int, elements: list[dict], *, prompt_landmark_id: str | None = None) -> str | None:
    return _context(index, elements, _lookup(elements), prompt_landmark_id)


def _grid(index, elements, lookup):
    row = elements[index]
    if row.get('role') != 'gridcell' or not re.fullmatch(r'\d+', row.get('name') or ''):
        return None
    ancestors, seen = [], {index}
    parent = _parent_index(row, lookup, len(elements))
    for _ in range(40):
        if parent is None or parent in seen or elements[parent].get('region') != row.get('region'):
            break
        seen.add(parent)
        ancestors.append(parent)
        parent = _parent_index(elements[parent], lookup, len(elements))
    table = next((i for i in ancestors if elements[i].get('role') in ('table', 'grid')), None)
    line = next((i for i in ancestors if elements[i].get('role') == 'row'), None)
    siblings = [i for i, e in enumerate(elements) if line is not None
                and _parent_index(e, lookup, len(elements)) == line and e.get('role') in ('gridcell', 'cell', 'rowheader')]
    siblings.sort(key=lambda i: ((_box(elements[i]) or [0])[0], i))
    lines = [i for i, e in enumerate(elements) if table is not None and e.get('role') == 'row'
             and _parent_index(e, lookup, len(elements)) == table]
    col = row.get('col_index', siblings.index(index)+1 if index in siblings else None)
    r = row.get('row_index', lines.index(line)+1 if line in lines else None)
    headers = [e for e in elements if e.get('role') == 'columnheader'
               and e.get('region') == row.get('region') and _container(e).get('id') == _container(row).get('id')]
    header = row.get('header')
    if not header and type(col) is int and 0 < col <= len(headers):
        header = headers[col-1].get('name')
    key = row.get('row_key') or (elements[siblings[0]].get('name') if siblings else None)
    # Use a structural table ordinal, never a producer id or anchor slug.
    table_ord = elements[table].get('table_ordinal') if table is not None else None
    return {'table': f'table#{table_ord}' if type(table_ord) is int else 'table',
            'row': r if type(r) is int else None, 'rows': len(lines) or None,
            'col': col if type(col) is int else None, 'cols': len(siblings) or None,
            'header': _clean_context_text(header), 'row_key': _clean_context_text(key)}


def _clean_context_text(text):
    if not isinstance(text, str) or re.search(r'(?i)(?:https?://|www\.|@)', text):
        return None
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', text)).strip()[:32] or None


def _wl_hashes(elements, adj):
    colors = [_sha(json.dumps([e.get('role'), canon_name(e.get('name'))[0], e.get('region')],
                             ensure_ascii=False)) for e in elements]
    for _ in range(2):
        colors = [_sha(json.dumps([colors[i], sorted((edge, colors[j]) for edge, j in adj.get(i, [])
                                                     if type(j) is int and 0 <= j < len(elements))]))
                  for i in range(len(elements))]
    return [color[:8] for color in colors]


# @anchor:wa.perceive.disambig.wl_signature
def wl_signature(index: int, elements: list[dict], adj: dict[int, list[tuple[str, int]]]) -> str:
    return _wl_hashes(elements, adj)[index]


def _adjacency(rows, lookup):
    adj = {i: [] for i in range(len(rows))}
    for i, row in enumerate(rows):
        parent = _parent_index(row, lookup, len(rows))
        if parent is not None and parent != i and _same_landmark(row, rows[parent]):
            adj[i].append(('parent', parent))
            adj[parent].append(('child', i))
        b = _box(row)
        for j in range(i):
            c = _box(rows[j])
            if b and c and _same_landmark(row, rows[j]) and abs((2*b[1]+b[3])-(2*c[1]+c[3])) <= 2*max(18, b[3], c[3]):
                adj[i].append(('row', j))
                adj[j].append(('row', i))
    return adj


def _subscope(row):
    for value in (row.get('scope'), _container(row).get('scope'), _container(row).get('role'), row.get('role')):
        if value in ('user_turn', 'code_block', 'code'):
            return 'code_block' if value == 'code' else value
    return None


def _inherited_subscope(index, rows, lookup):
    row, seen = rows[index], set()
    for _ in range(40):
        if index in seen:
            break
        seen.add(index)
        candidate = rows[index]
        if candidate.get('region') != row.get('region'):
            break
        scope = _subscope(candidate)
        if scope:
            return scope
        index = _parent_index(candidate, lookup, len(rows))
        if index is None:
            break
    return None


def _answer_marker(row):
    role, name = row.get('role'), row.get('name') or ''
    if row.get('region') != 'main' or _subscope(row):
        return False
    return ((role == 'button' and re.match(r'^Investigado\b', name, re.I))
            or (role == 'heading' and re.match(r'^(ChatGPT dijo|Gemini ha dicho)\s*:?', name, re.I))
            or (role == 'article' and (row.get('author') == 'assistant'
                                     or _container(row).get('role') == 'assistant')))


def _scopes(rows):
    markers = {}
    for row in rows:
        if _answer_marker(row):
            cid, ordinal = _container(row).get('id'), _container(row).get('ordinal')
            if isinstance(cid, str) and type(ordinal) is int:
                markers[cid] = (ordinal, row)
    ordered = sorted(markers, key=lambda cid: (markers[cid][0], cid))
    scopes = {}
    for k, cid in enumerate(ordered, 1):
        marker = markers[cid][1]
        if k < len(ordered):
            scopes[cid] = f'answer:{k}-of-{len(ordered)},non_last'
        elif marker.get('role') == 'button':
            scopes[cid] = 'after:last(button "Investigado*")'
        elif (marker.get('name') or '').startswith('ChatGPT'):
            scopes[cid] = 'after:last(heading "ChatGPT dijo:")'
        else:
            scopes[cid] = 'after:last(heading "Gemini ha dicho")'
    return scopes


# @anchor:wa.perceive.disambig.disambiguate
def disambiguate(rows: list[dict], *, token_cap_bytes: int = 48) -> list[dict]:
    """Group once, order within each group, then hash the shared graph once.

    ``_elements`` optionally supplies uncapped structural context from the surface;
    only generated option ids are emitted. Inputs are never mutated.
    """
    if type(token_cap_bytes) is not int or token_cap_bytes < 1:
        raise ValueError('invalid affix budget')
    elements = rows[0].get('_elements', rows) if rows else rows
    lookup = _lookup(elements)
    indices = {id(e): i for i, e in enumerate(elements)}
    scopes = _scopes(elements)
    groups = {}
    for i, row in enumerate(rows):
        groups.setdefault((row.get('role'), canon_name(row.get('name'))[0]), []).append(i)
    hashes = _wl_hashes(rows, _adjacency(rows, _lookup(rows))) if any(len(g) > 1 for g in groups.values()) else []
    composites = container_vs_leaf(rows)
    result = [{} for _ in rows]
    for group in groups.values():
        def order(i):
            ordinal = _container(rows[i]).get('ordinal', rows[i].get('container_ordinal'))
            box = _box(rows[i]) or [0, 0, 0, 0]
            return (ordinal if type(ordinal) is int else 10**18, box[1], box[0], i)
        group.sort(key=order)
        for k, index in enumerate(group, 1):
            row = rows[index]
            original = row.get('_source', row)
            doc_index = indices.get(id(original), index)
            ctx = _context(doc_index, elements, lookup)
            item = {'ordinal_1k': k, 'cardinality': len(group), 'tag': f'{k}-of-{len(group)}',
                    'container_ctx': ctx, 'scope': _inherited_subscope(doc_index, elements, lookup) or scopes.get(_container(row).get('id')),
                    'neighbor_prev': rows[group[k-2]].get('option_id', f'e{group[k-2]}') if k > 1 else None,
                    'neighbor_next': rows[group[k]].get('option_id', f'e{group[k]}') if k < len(group) else None,
                    'wl_sig': hashes[index] if len(group) > 1 else None}
            grid = _grid(doc_index, elements, lookup)
            if grid:
                item['row_col_header'] = grid
            if index in composites:
                item['child_actions'] = [{'id': rows[j].get('option_id', f'e{j}'),
                                          'name': str(rows[j].get('name') or '')[:120]} for j in composites[index]]
            result[index] = item
    return result


def _cut(text, budget):
    return text.encode('utf-8')[:max(0, budget)].decode('utf-8', errors='ignore')


# @anchor:wa.perceive.disambig.format_label
def format_label(option: dict, disambig_row: dict, *, max_suffix_bytes: int = 48) -> str:
    name = option.get('name') or ''
    d = disambig_row
    children, grid = d.get('child_actions'), d.get('row_col_header')
    if children:
        for child in children:
            name = name.replace(child['name'], '').strip()
    if d['cardinality'] == 1 and not children and not grid:
        return name
    head = f"[{d['ordinal_1k']}/{d['cardinality']}"
    ctx = d.get('scope') or d.get('container_ctx') or ''
    if children:
        ctx = 'container: select_row; child_actions -> ' + ','.join(c['id'] for c in children)
    if grid:
        ctx = f"{grid['table']} row={grid['row']}/{grid['rows']} col={grid['col']}/{grid['cols']}"
    # Ordinals are first, so every nonempty supported prefix distinguishes a
    # collision regardless of Unicode context length. Never emit half a tag.
    if type(max_suffix_bytes) is not int or max_suffix_bytes < len((head + '] ').encode()):
        raise ValueError('affix budget cannot fit ordinal')
    ctx = _cut(ctx, max_suffix_bytes-len((head + ' ] ').encode()))
    return head + (' ' + ctx if ctx else '') + '] ' + name


# @anchor:wa.perceive.disambig.TurnOwnership
@dataclass(frozen=True)
class TurnOwnership:
    turn_index: int
    prompt_echo_hash: str
    prev_answer_sha256: str | None
    boundary_max_ordinal: int | None
    boundary_containers: frozenset[str]
    prompt_norm_prefix_sha256: str | None = None

    def __post_init__(self):
        if not isinstance(self.prompt_echo_hash, str):
            raise ValueError('missing prompt digest')
        if type(self.turn_index) is not int or self.turn_index < 1:
            raise ValueError('invalid turn index')
        for value in (self.prompt_echo_hash, self.prev_answer_sha256, self.prompt_norm_prefix_sha256):
            if value is not None and not re.fullmatch(r'[0-9a-f]{64}', value):
                raise ValueError('invalid ownership digest')
        if self.boundary_max_ordinal is not None and type(self.boundary_max_ordinal) is not int:
            raise ValueError('invalid boundary ordinal')
        if not isinstance(self.boundary_containers, frozenset) or not all(isinstance(c, str) for c in self.boundary_containers):
            raise ValueError('invalid boundary containers')


# @anchor:wa.perceive.disambig.capture_turn_boundary
def capture_turn_boundary(ax_snap: dict, *, turn_index: int = 1, task_text: str = '', prev_answer_sha256: str | None = None) -> TurnOwnership:
    ids, ordinals = set(), []
    for row in ax_snap.get('elements', []):
        if row.get('region') != 'main':
            continue
        c = _container(row)
        if isinstance(c.get('id'), str):
            ids.add(c['id'])
        if _answer_marker(row) and type(c.get('ordinal')) is int:
            ordinals.append(c['ordinal'])
    prompt_hash = _sha(_norm(task_text))
    return TurnOwnership(turn_index, prompt_hash, prev_answer_sha256,
                         max(ordinals) if ordinals else None, frozenset(ids), prompt_hash)


# @anchor:wa.perceive.disambig.answer_container_for_turn
def answer_container_for_turn(ax_snap: dict, ownership: TurnOwnership | None = None) -> str | None:
    rows = ax_snap.get('elements', [])
    candidates, terminals, contents = [], {}, {}
    lookup = _lookup(rows)
    for i, row in enumerate(rows):
        if row.get('region') != 'main':
            continue
        if ((row.get('states') or {}).get('busy') is True
                or (row.get('role') == 'button' and
                    (canon_name(row.get('name'))[1] or re.match(r'^Investigando\b', row.get('name') or '', re.I)))):
            return None
        c = _container(row)
        cid, ordinal = c.get('id'), c.get('ordinal')
        if not isinstance(cid, str) or not cid or type(ordinal) is not int:
            continue
        scope = _inherited_subscope(i, rows, lookup)
        if _answer_marker(row) and not scope:
            candidates.append((ordinal, i, cid, row))
        if row.get('role') == 'button' and _TERMINAL.fullmatch(row.get('name') or '') and not scope:
            terminals[cid] = i
        if row.get('role') in ('heading', 'article', 'paragraph', 'text', 'StaticText'):
            contents.setdefault(cid, []).append(row)
    eligible = [c for c in candidates if ownership is None or
                (c[2] not in ownership.boundary_containers and
                 (ownership.boundary_max_ordinal is None or c[0] > ownership.boundary_max_ordinal))]
    if not eligible:
        return None
    _, index, cid, marker = max(eligible, key=lambda c: (c[0], c[1]))
    if terminals.get(cid, -1) <= index:
        return None
    content = contents.get(cid, [])
    if content:
        body = [e for e in content if e.get('role') != 'heading' and (e.get('name') or e.get('preview'))]
        if not body:
            return None
        if ownership and all(_sha(_norm(e.get('name') or e.get('preview'))) == ownership.prompt_echo_hash for e in body):
            return None
    return cid


# @anchor:wa.perceive.disambig.verify_harvested_text
def verify_harvested_text(text: str | None, ownership: TurnOwnership | None = None) -> tuple[bool, str | None, str | None]:
    if text is None or not text.strip():
        return False, None, 'empty_text'
    digest, norm = _sha(text), _norm(text)
    norm_digest = _sha(norm)
    if ownership and ownership.prev_answer_sha256 in (digest, norm_digest, _sha(text.strip()), _sha(re.sub(r'\s+', ' ', unicodedata.normalize('NFC', text)).strip())):
        return False, digest, 'stale_prev_answer_sha256'
    if ownership and norm_digest == ownership.prompt_echo_hash:
        return False, digest, 'question_echo'
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) == 1 and re.fullmatch(r'(?:#+\s*[^\n]+|(?:ChatGPT dijo|Gemini ha dicho|Investigado[^:\n]*):?)', lines[0], re.I):
        return False, digest, 'heading_only'
    return True, digest, None


@dataclass(frozen=True)
class _ScopeCompletenessEvidence:
    scope_complete: bool
    turn_owned: bool
    answer_complete: bool | None
    selected_turn_id: str | None
    turn_index: int
    reason: str
    candidate_count: int


def _verify_complete_scope_turn(turns, *, expected_turn_id=None, require_complete=True,
                                scope_complete_flag=None, stream_truncated=False):
    # This checkout has answer_container_for_turn rather than resolve_answer_turn.
    # The S4 seam accepts explicit host turn records without altering legacy APIs.
    if (scope_complete_flag is not True or stream_truncated is True
            or any(not isinstance(t, dict) or t.get('partial_scope') is True
                   or t.get('truncated') is True or t.get('scope_complete') is False for t in turns)):
        return _ScopeCompletenessEvidence(False, False, None, None, -1,
                                          'PARTIAL_SUBTREE_OR_TRUNCATED_SCOPE', len(turns))
    if not turns:
        return _ScopeCompletenessEvidence(True, False, None, None, -1, 'NO_TURN_EVIDENCE', 0)
    # Latest explicit order is authority, never tuple order or a guessed ordinal.
    if any(type(t.get('order_index')) is not int or t['order_index'] < 0
           or not isinstance(t.get('turn_id'), str) or not t['turn_id'] for t in turns):
        return _ScopeCompletenessEvidence(True, False, None, None, -1, 'INVALID_TURN', len(turns))
    latest = max(t['order_index'] for t in turns)
    candidates = [t for t in turns if t['order_index'] == latest]
    if len(candidates) != 1:
        return _ScopeCompletenessEvidence(True, False, None, None, -1, 'AMBIGUOUS_TURN', len(turns))
    turn = candidates[0]
    if turn.get('streaming') is not False or (require_complete and turn.get('complete') is not True):
        return _ScopeCompletenessEvidence(True, False, False, None, -1, 'STREAMING_INCOMPLETE', len(turns))
    if expected_turn_id is not None and turn['turn_id'] != expected_turn_id:
        return _ScopeCompletenessEvidence(True, False, None, None, -1, 'TURN_OWNERSHIP_MISMATCH', len(turns))
    return _ScopeCompletenessEvidence(True, True, True, turn['turn_id'], latest, 'OK', len(turns))


def _verify_landmark_containment(target_node, container_node):
    target, box = target_node.get('bbox'), container_node.get('bbox')
    key = container_node.get('element_id', container_node.get('id'))
    linked = key is not None and (target_node.get('container_id') == key
        or target_node.get('landmark_id') == key or key in target_node.get('ancestor_ids', ()))
    geometric = (target is not None and box is not None and len(target) == len(box) == 4
        and all(type(v) is int for v in (*target, *box))
        and target[2] > 0 and target[3] > 0 and box[2] > 0 and box[3] > 0
        and box[0] <= target[0] and box[1] <= target[1]
        and target[0]+target[2] <= box[0]+box[2] and target[1]+target[3] <= box[1]+box[3])
    return (True, 'OK') if linked and geometric else (False, 'LANDMARK_CONTAINMENT_UNVERIFIED')
