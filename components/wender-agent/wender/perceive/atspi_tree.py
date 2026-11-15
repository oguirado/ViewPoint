# @module {"id":"wa.perceive.atspi_tree","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.atspi","wa.perceive.ax","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Bounded desktop accessibility walk producing prune_ax_v1 nodes and STATE.

Transport and child parsing come from atspi; normalization, IDs and hashes come
from ax. No desktop connection is opened until the supplied call is invoked.
"""
import ast
import re

from wender.perceive import atspi, ax
from wender.state import schema


# @anchor:wa.perceive.atspi_tree.ATSPI_ROLE_MAP
ATSPI_ROLE_MAP = {
    'push button': 'button', 'toggle button': 'button', 'check box': 'checkbox',
    'radio button': 'radio', 'text': 'textbox', 'entry': 'textbox',
    'combo box': 'combobox', 'list': 'listbox', 'list item': 'option',
    'menu bar': 'menubar', 'menu': 'menu', 'menu item': 'menuitem',
    'page tab list': 'tablist', 'page tab': 'tab', 'slider': 'slider',
    'spin button': 'spinbutton', 'tool bar': 'toolbar', 'dialog': 'dialog',
    'heading': 'heading', 'link': 'link', 'frame': 'application',
    'window': 'application',
}
_ACCESSIBLE = 'org.a11y.atspi.Accessible'
_STRING = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")
# org.a11y.atspi.StateType indices in the GetState array of uint32 words.
_STATE_BITS = {'busy': 3, 'checked': 4, 'expandable': 9, 'expanded': 10,
               'pressed': 19, 'selectable': 21, 'selected': 22, 'sensitive': 23}


def _string(raw):
    match = _STRING.search(raw or '')
    if match:
        try:
            return ast.literal_eval(match.group())
        except (SyntaxError, ValueError):
            pass
    return ''


def _state_bits(raw):
    match = re.search(r'\[([^\]]*)\]', raw or '')
    if not match:
        return None
    body = re.sub(r'\buint32\s+', '', match.group(1))
    words = body.split(',')
    if not words or any(not re.fullmatch(r'\s*(?:0x[0-9a-fA-F]+|[0-9]+)\s*', word)
                        for word in words):
        return None
    values = [int(word.strip(), 16 if word.strip().startswith('0x') else 10) for word in words]
    if any(word > 0xffffffff for word in values):
        return None
    return sum(word << (32 * i) for i, word in enumerate(values))


def _attrs(role, raw):
    bits = _state_bits(raw)
    if bits is None:
        return ()
    has = lambda name: bool(bits & (1 << _STATE_BITS[name]))
    attrs = {key: 'true' for key in ('busy', 'checked', 'expanded', 'pressed', 'selected') if has(key)}
    if not has('sensitive'):
        attrs['disabled'] = 'true'
    if role in ('check box', 'radio button'):
        attrs['checked'] = 'true' if has('checked') else 'false'
    if role == 'toggle button':
        attrs['pressed'] = 'true' if has('pressed') else 'false'
    if has('expandable'):
        attrs['expanded'] = 'true' if has('expanded') else 'false'
    if has('selectable'):
        attrs['selected'] = 'true' if has('selected') else 'false'
    return tuple((key, attrs[key]) for key in ax.STATE_ATTRS if key in attrs)


# @anchor:wa.perceive.atspi_tree.walk
def walk(address, bus, path, *, call=atspi._call, max_nodes=2000, max_depth=40, timeout=10):
    """Preorder traversal, skipping repeated (bus, path) identities.

    Limits count raw accessibles and raw edges from the root (depth zero),
    respectively. Emitted depth counts only emitted ancestors, as in ax.prune.
    Truncation means an unvisited accessible was omitted by a limit; a leaf
    exactly at a limit and a previously visited cyclic edge are not truncation.
    """
    for name, value in (('max_nodes', max_nodes), ('max_depth', max_depth)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(name + ' must be a non-negative integer')
    nodes, seen = [], set()
    raw_count, truncated = 0, False
    stack = [(bus, path, 0, ())]
    while stack:
        dest, obj_path, raw_depth, ancestors = stack.pop()
        identity = (dest, obj_path)
        if identity in seen:
            continue
        if raw_depth > max_depth:
            truncated = True
            continue
        if raw_count >= max_nodes:
            truncated = True
            break
        seen.add(identity)
        raw_count += 1
        role = _string(call(address, dest, obj_path, _ACCESSIBLE + '.GetRoleName', timeout=timeout))
        chassis = ATSPI_ROLE_MAP.get(role)
        if chassis is not None:
            name = _string(call(address, dest, obj_path, 'org.freedesktop.DBus.Properties.Get',
                                _ACCESSIBLE, 'Name', timeout=timeout))
            attrs = _attrs(role, call(address, dest, obj_path, _ACCESSIBLE + '.GetState', timeout=timeout))
            nodes.append({'role': chassis, 'name': ax.normalize_name(name), 'attrs': attrs,
                          'depth': len(ancestors), 'ancestors': ancestors})
            ancestors += (chassis,)
        children = atspi.parse_children(call(address, dest, obj_path, _ACCESSIBLE + '.GetChildren',
                                             timeout=timeout))
        for child_bus, child_path in reversed(children):
            stack.append((child_bus, child_path, raw_depth + 1, ancestors))
    counts = {}
    for node in nodes:
        key = (node['role'], node['name'])
        node['ordinal'] = counts.get(key, 0)
        counts[key] = node['ordinal'] + 1
    for node in nodes:
        node['cardinality'] = counts[(node['role'], node['name'])]
        node['id'] = ax.stable_id(node)
    return {'nodes': nodes, 'raw_count': raw_count, 'truncated': truncated}


# @anchor:wa.perceive.atspi_tree.atspi_state
def atspi_state(result, title):
    """Build a desktop STATE with unknown geometry and canonical AX hashes."""
    nodes = result['nodes']
    return schema.build_state(
        kind='atspi', url='', title=ax.normalize_name(title),
        prune={'name': ax.PRUNE_NAME, 'version': ax.PRUNE_VERSION},
        nodes=nodes, raw_count=result['raw_count'],
        ax_sha256=ax.snapshot_sha(nodes), ax_stable_sha256=ax.snapshot_sha(nodes, with_state=False),
        actionable_roles=ax.ACTIONABLE_ROLES, boxes=None, viewport=None)
