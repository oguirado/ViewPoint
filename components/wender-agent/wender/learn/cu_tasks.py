# @module {"id":"wa.learn.cu_tasks","layer":2,"owner":"LEARN","deps":["wa.perceive.srf_identity"],"book":"HB-WA-s4-learn"}
"""Holdings task predicates, ported from app/shell/tools/cu_task_check.js.

Only typed comparisons and boolean composition are accepted. Missing current
frame evidence yields None, including under negation or an otherwise true OR.
"""
import json
import operator
from pathlib import Path
import re
import unicodedata

# The task corpus (tasks.v1.jsonl) ships with the Holdings demo app, not with this package; point WENDER_CU_TASKS_FILE at it.
DEFAULT_TASKS_FILE = __import__('os').environ.get('WENDER_CU_TASKS_FILE', 'tasks.v1.jsonl')
_FIELDS = dict.fromkeys(('tick', 'revision', 'cash_minor', 'employed', 'inbox_open'), 'int')
_FIELDS.update(route='str', focus='str')
_OPS = {'==': operator.eq, '!=': operator.ne, '<': operator.lt,
        '<=': operator.le, '>': operator.gt, '>=': operator.ge}
_SAFE_INT = 9007199254740991


def _tokens(src):
    if not isinstance(src, str):
        raise ValueError('predicate must be text')
    i = 0
    out = []
    escapes = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f',
               '/': '/', '\\': '\\', '"': '"', "'": "'"}
    while i < len(src):
        c = src[i]
        if c in ' \t\r\n':
            i += 1
            continue
        if c in "\"'":
            quote, value = c, ''
            i += 1
            while i < len(src) and src[i] != quote:
                c = src[i]
                if c in '\r\n':
                    raise ValueError('unterminated string')
                i += 1
                if c == '\\':
                    if i >= len(src):
                        raise ValueError('unterminated escape')
                    c = src[i]
                    i += 1
                    if c == 'u':
                        digits = src[i:i + 4]
                        if re.fullmatch('[0-9a-fA-F]{4}', digits) is None:
                            raise ValueError('bad unicode escape')
                        c = chr(int(digits, 16))
                        i += 4
                    elif c in escapes:
                        c = escapes[c]
                    else:
                        raise ValueError('bad escape')
                value += c
            if i == len(src):
                raise ValueError('unterminated string')
            i += 1
            # Match JavaScript's UTF-16 string identity for escaped pairs.
            value = value.encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'surrogatepass')
            out.append(('str', value))
            continue
        match = re.match(r'-?[0-9]+|[A-Za-z_][A-Za-z_0-9]*|==|!=|<=|>=|[<>()]', src[i:])
        if match is None:
            raise ValueError('invalid predicate token')
        word = match.group()
        i += len(word)
        if re.fullmatch('-?[0-9]+', word):
            if i < len(src) and re.match(r'[.A-Za-z_]', src[i]):
                raise ValueError('invalid integer literal')
            value = int(word)
            if abs(value) > _SAFE_INT:
                raise ValueError('integer out of range')
            out.append(('int', value))
        else:
            out.append(('word', word))
    return out


def _type(node):
    kind = node[0]
    if kind in ('int', 'str'):
        return kind
    if kind == 'field':
        return _FIELDS[node[1]]
    types = [_type(child) for child in node[1:] if isinstance(child, tuple)]
    if kind in ('and', 'or', 'not'):
        if any(t != 'bool' for t in types):
            raise ValueError('boolean operands required')
    elif types[0] != types[1] or types[0] == 'bool':
        raise ValueError('incompatible comparison operands')
    elif types[0] == 'str' and kind not in ('==', '!='):
        raise ValueError('ordering requires integers')
    return 'bool'


# @anchor:wa.learn.cu_tasks.parse_predicate
def parse_predicate(source):
    """Parse and type-check the checker grammar; return an inert tuple tree."""
    tokens = _tokens(source)
    pos = 0

    def peek(word):
        return pos < len(tokens) and tokens[pos] == ('word', word)

    def primary():
        nonlocal pos
        if pos == len(tokens):
            raise ValueError('unexpected end of predicate')
        kind, value = tokens[pos]
        pos += 1
        if kind in ('int', 'str'):
            return (kind, value)
        if value == '(':
            node = expression('or')
            if not peek(')'):
                raise ValueError('expected closing parenthesis')
            pos += 1
            return node
        if value not in _FIELDS:
            raise ValueError('unknown field or misplaced token: ' + value)
        return ('field', value)

    def comparison():
        nonlocal pos
        node = primary()
        if pos < len(tokens) and tokens[pos][1] in _OPS and tokens[pos][0] == 'word':
            op = tokens[pos][1]
            pos += 1
            node = (op, node, primary())
        return node

    def negate():
        nonlocal pos
        if peek('not'):
            pos += 1
            return ('not', negate())
        return comparison()

    def expression(op):
        nonlocal pos
        next_level = (lambda: expression('and')) if op == 'or' else negate
        node = next_level()
        while peek(op):
            pos += 1
            node = (op, node, next_level())
        return node

    try:
        tree = expression('or')
        if pos != len(tokens) or _type(tree) != 'bool':
            raise ValueError('predicate must be one boolean expression')
        return tree
    except RecursionError as exc:
        raise ValueError('predicate nesting too deep') from exc


# @anchor:wa.learn.cu_tasks.evaluate_predicate
def evaluate_predicate(predicate, status):
    """Return True/False, or None if any referenced field lacks typed evidence."""
    tree = parse_predicate(predicate) if isinstance(predicate, str) else predicate

    def value(node):
        kind = node[0]
        if kind in ('str', 'int'):
            return node[1]
        if kind == 'field':
            item = status.get(node[1])
            expected = int if _FIELDS[node[1]] == 'int' else str
            if type(item) is not expected or (expected is int and abs(item) > _SAFE_INT):
                return None
            return item
        left = value(node[1])
        if kind == 'not':
            return None if left is None else not left
        right = value(node[2])
        if left is None or right is None:
            return None
        if kind == 'and':
            return left and right
        if kind == 'or':
            return left or right
        return _OPS[kind](left, right)

    return value(tree)


# @anchor:wa.learn.cu_tasks.predicate_distance
def predicate_distance(predicate, env):
    """Distance to truth; unknown evidence stays unknown, including under OR.

    Integer comparisons use their gap, categorical comparisons one mismatch.
    AND sums distances, OR takes the minimum; NOT reverses the target truth.
    """
    tree = parse_predicate(predicate) if isinstance(predicate, str) else predicate
    if evaluate_predicate(tree, env) is None:
        return None

    def operand(node):
        return env[node[1]] if node[0] == 'field' else node[1]

    def distance(node, desired=True):
        kind = node[0]
        if kind == 'not':
            return distance(node[1], not desired)
        if kind in ('and', 'or'):
            parts = [distance(n, desired) for n in node[1:]]
            return sum(parts) if (kind == 'and') == desired else min(parts)
        left, right = operand(node[1]), operand(node[2])
        if _OPS[kind](left, right) == desired:
            return 0
        if type(left) is not int:
            return 1
        op = kind if desired else {'==': '!=', '!=': '==', '<': '>=',
                                   '<=': '>', '>': '<=', '>=': '<'}[kind]
        if op == '==':
            return abs(left - right)
        if op == '!=':
            return 1
        if op in ('<', '<='):
            return left - right + (op == '<')
        return right - left + (op == '>')

    return distance(tree)


# @anchor:wa.learn.cu_tasks.task_family_hint
def task_family_hint(task_id):
    """Hints belong to the task family, outside the generic model prompt."""
    if task_id and task_id.startswith('hb_tf_'):
        return ("Holdings inbox: opening a message is 'Mark read'; 'Acknowledge' "
                "is only allowed after Mark read, and only do it when the goal asks "
                "to acknowledge/confirm/clear/handle. Clicking a row only moves focus.")
    return ''


# @anchor:wa.learn.cu_tasks.render_predicate
def render_predicate(predicate):
    """Render typed predicate structure without losing boolean grouping."""
    tree = parse_predicate(predicate) if isinstance(predicate, str) else predicate
    words = {'==': 'is', '!=': 'is not', '<': 'is less than', '<=': 'is at most',
             '>': 'is greater than', '>=': 'is at least', 'and': 'and', 'or': 'or'}

    def render(node):
        if node[0] in ('str', 'int', 'field'):
            return str(node[1])
        if node[0] == 'not':
            return 'not (' + render(node[1]) + ')'
        return '(' + render(node[1]) + ' ' + words[node[0]] + ' ' + render(node[2]) + ')'
    return 'success means: ' + render(tree)


# @anchor:wa.learn.cu_tasks.extract_status
def extract_status(frame):
    """Extract C5 evidence from a raw or normalized CURRENT C1 frame.

    Owner status is identified through its projection anchor, not arbitrary node
    names. Route is the structured source title; focus is a focused node's anchor.
    No focused node means unknown, not an invented empty focus string.
    """
    if not isinstance(frame, dict):
        return {}
    frame = frame.get('frame', frame) if 'start' in frame else frame
    lane = frame.get('lane', frame.get('state', {}))
    if not isinstance(lane, dict):
        return {}
    structured = lane.get('state', {})
    if not isinstance(structured, dict):
        structured = {}
    nodes = structured.get('nodes', [])
    elements = lane.get('elements', [])
    if not isinstance(nodes, list) or not isinstance(elements, list):
        return {}
    anchors = {e.get('state_node_id'): e.get('anchor', e.get('name'))
               for e in elements if isinstance(e, dict) and e.get('state_node_id')}
    owners = [n.get('states', {}) for n in nodes if isinstance(n, dict)
              and n.get('role') == 'status'
              and anchors.get(n.get('id')) == '/holdings/owner/projection']
    owner = owners[0] if len(owners) == 1 and isinstance(owners[0], dict) else {}
    result = {}
    for key in _FIELDS:
        candidates = [source[key] for source in (lane, owner) if key in source]
        if key == 'route':
            source = structured.get('source', {})
            if isinstance(source, dict) and 'title' in source:
                candidates.append(source['title'])
        if key == 'focus':
            focused = [n for n in nodes if isinstance(n, dict) and
                       isinstance(n.get('states'), dict) and n['states'].get('focused') in ('true', True)]
            if len(focused) == 1:
                candidates.append(anchors.get(focused[0].get('id')))
        normalized = []
        for item in candidates:
            if _FIELDS[key] == 'int' and isinstance(item, str) and re.fullmatch('-?[0-9]+', item):
                item = int(item)
            expected = int if _FIELDS[key] == 'int' else str
            if type(item) is not expected or (expected is int and abs(item) > _SAFE_INT):
                normalized.append(None)
            else:
                normalized.append(item)
        if normalized and normalized[0] is not None and all(x == normalized[0] for x in normalized):
            result[key] = normalized[0]
    return result


# @anchor:wa.learn.cu_tasks.load_task
def load_task(task_id, tasks_file=DEFAULT_TASKS_FILE):
    """Select a unique task line and reject unusable goal/budget/predicate data."""
    tasks = {}
    for line in Path(tasks_file).read_text(encoding='utf-8').splitlines():
        task = json.loads(line)
        if not isinstance(task, dict) or not isinstance(task.get('task_id'), str):
            raise ValueError('task must have a task_id')
        if task['task_id'] in tasks:
            raise ValueError('duplicate task_id: ' + task['task_id'])
        tasks[task['task_id']] = task
    if task_id not in tasks:
        raise ValueError('unknown task_id: ' + task_id)
    task = tasks[task_id]
    for key in ('goal', 'scenario', 'success_predicate'):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise ValueError('missing task ' + key)
    for key in ('seed', 'max_actions'):
        if type(task.get(key)) is not int or task[key] < (1 if key == 'max_actions' else 0):
            raise ValueError('invalid task ' + key)
    parse_predicate(task['success_predicate'])
    return task


# @anchor:wa.learn.cu_tasks.observation_status
def observation_status(frame):
    """Bounded current status; unavailable evidence is null, never zero.

    Status labels are observation text, not instructions. Retain them so a
    refusal rendered as a label is visible even without a dedicated code field.
    """
    status = extract_status(frame)
    if not isinstance(frame, dict):
        frame = {}
    frame = frame.get('frame', frame) if 'start' in frame else frame
    lane = frame.get('lane', frame.get('state', {}))
    lane = lane if isinstance(lane, dict) else {}
    structured = lane.get('state', {})
    structured = structured if isinstance(structured, dict) else {}
    nodes = structured.get('nodes', [])
    nodes = [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []
    sources = [lane] + [n['states'] for n in nodes if isinstance(n.get('states'), dict)]
    inbox_fields = ('unread_count', 'read_count', 'acked_count', 'unread', 'read', 'acked')
    for key in (*inbox_fields, 'last_refusal_code'):
        values = [source[key] for source in sources if key in source]
        if values and all(v == values[0] for v in values):
            value = values[0]
            if key in inbox_fields and isinstance(value, str) and value.isdecimal() and len(value) <= 16:
                value = int(value)
            if key in ('unread', 'read', 'acked') and type(value) is bool:
                status[key] = value
            elif key in inbox_fields and type(value) is int and 0 <= value <= _SAFE_INT:
                status[key] = value
            elif key == 'last_refusal_code' and isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', value):
                status[key] = value
    labels = [n['name'][:300] for n in nodes if n.get('role') == 'status'
              and isinstance(n.get('name'), str)][:16]
    if 'last_refusal_code' not in status:
        codes = [match.group(1) for label in labels for match in
                 re.finditer(r'(?i:refusal|refused)\s*[:=]?\s*([A-Z][A-Z0-9_]{0,63})\b', label)]
        if codes and len(set(codes)) == 1:
            status['last_refusal_code'] = codes[0]
    result = {key: status.get(key) for key in (*_FIELDS, *inbox_fields, 'last_refusal_code')}
    result['status_text'] = labels
    return result


def _norm_text(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', value)).strip().casefold()


# @anchor:wa.learn.cu_tasks.web_predicate
def web_predicate(rule, ax, *, before=None, harvested=None):
    """Web postchecks are inert mappings. Missing evidence remains unknown (None)."""
    kind = rule['kind']
    if kind == 'contains_text':
        substr = rule.get('substr')
        if not isinstance(substr, str) or not substr:
            raise ValueError('contains_text requires a non-empty substr')
        needle = _norm_text(substr)
        if not needle:
            raise ValueError('contains_text substr normalizes to empty')
    if kind in ('all', 'any'):
        values = [web_predicate(r, ax, before=before, harvested=harvested) for r in rule['rules']]
        if not values or None in values:
            return None
        return all(values) if kind == 'all' else any(values)
    if kind == 'text_nonempty':
        return bool(harvested.strip()) if isinstance(harvested, str) else None
    if kind in ('url', 'title'):
        value = (ax.get('window') or {}).get(kind, ax.get(kind))
        return bool(re.search(rule['pattern'], value)) if isinstance(value, str) else None
    elements = ax.get('elements')
    if not isinstance(elements, list):
        return None
    def matches(e):
        if not (('role' not in rule or e.get('role') == rule['role'])
                and ('region' not in rule or e.get('region') == rule['region'])
                and re.search(rule.get('pattern', '.*'), e.get('name') if isinstance(e.get('name'), str) else '') is not None):
            return False
        states = e.get('states') if isinstance(e.get('states'), dict) else {}
        expected = rule.get('states', {})
        if any(type(states.get(k)) is not bool or type(v) is not bool for k, v in expected.items()):
            return None
        return all(states[k] is v for k, v in expected.items())
    matched = [matches(e) for e in elements]
    if kind in ('element_present', 'element_absent'):
        found = True if True in matched else None if None in matched else False
        return found if kind == 'element_present' or found is None else not found
    if kind == 'value_nonempty':
        found = [e for e, match in zip(elements, matched) if match is True]
        if None in matched or not found or any(type(e.get('value_len')) is not int or e['value_len'] < 0 for e in found):
            return None
        return any(e['value_len'] > 0 for e in found)
    if kind == 'contains_text':
        found = [e for e, match in zip(elements, matched) if match is True]
        if None in matched or not found:
            return None
        haystacks = []
        for element in found:
            texts = [element.get('name'), element.get('text')]
            if not any(isinstance(text, str) for text in texts):
                return None
            haystacks.extend(_norm_text(text) for text in texts if isinstance(text, str))
        return any(needle in hay for hay in haystacks)
    if kind == 'container_count_increased':
        if not isinstance(before, dict) or not isinstance(before.get('elements'), list):
            return None
        def count(rows):
            if any(matches(e) is None for e in rows):
                return None
            return len({e['container']['id'] for e in rows if matches(e) is True
                        and (e.get('container') or {}).get('id') is not None})
        current, previous = count(elements), count(before['elements'])
        return current > previous if current is not None and previous is not None else None
    if kind == 'answer_complete':
        from wender.perceive.srf_identity import last_answer_container
        busy = any(e.get('role') == 'button' and re.search(r'Detener|Investigando|Stop response', e.get('name', ''), re.I)
                   for e in elements)
        container = last_answer_container(ax)
        if before is not None and last_answer_container(before) == container:
            return False
        return not busy and container is not None
    raise ValueError('unknown web predicate')
