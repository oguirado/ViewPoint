# @module {"id":"wa.perceive.osc2","layer":1,"owner":"PERCEIVE","deps":[],"book":"HB-WA-s1-perceive"}
"""Join client-area OSC2 geometry using independently corroborated vision marks."""
import math
from statistics import median


def _box(value):
    if isinstance(value, dict):
        value = [value.get(k) for k in ('x', 'y', 'w', 'h')]
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)
            or value[2] <= 0 or value[3] <= 0):
        return None
    return value


def _iou(a, b):
    area = max(0, min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0])) * max(
        0, min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]))
    return area / (a[2]*a[3]+b[2]*b[3]-area)


def _estimate(nodes, marks):
    boxes = [_box(n.get('box')) for n in nodes if n.get('actionable') is True
             and n.get('states', {}).get('visible') not in (False, 'false')
             and n.get('states', {}).get('enabled') not in (False, 'false')]
    boxes = [b for b in boxes if b]
    marks = [_box(m.get('bbox', m)) for m in marks]
    marks = [b for b in marks if b]
    candidates = {}
    # Only nearby origin deltas can support an estimate. Keep the sparse edges
    # once rather than recomputing every node/mark cross product per candidate.
    edges_by_shift = {}
    for i, a in enumerate(boxes):
        for j, b in enumerate(marks):
            dx, dy = b[0]-a[0], b[1]-a[1]
            if -13 <= dx <= 13 and -3 <= dy <= 123:
                edges_by_shift.setdefault((dx, dy), []).append((i, j))
    for dx, dy in edges_by_shift:
        if not (-10 <= dx <= 10 and 0 <= dy <= 120):
            continue
        nearby = [(i, j) for (ex, ey), pairs in edges_by_shift.items()
                  if math.hypot(ex-dx, ey-dy) <= 3 for i, j in pairs]
        edges = sorted(((_iou([boxes[i][0]+dx, boxes[i][1]+dy, *boxes[i][2:]], marks[j]), i, j)
                        for i, j in nearby), reverse=True)
        used_n, used_m, residuals = set(), set(), []
        for score, i, j in edges:
            residual = math.hypot(marks[j][0]-boxes[i][0]-dx, marks[j][1]-boxes[i][1]-dy)
            if score >= .5 and residual <= 3 and i not in used_n and j not in used_m:
                used_n.add(i)
                used_m.add(j)
                residuals.append(residual)
        if len(residuals) >= 3:
            candidates[dx, dy] = (len(residuals), -median(residuals))
    if not candidates:
        raise ValueError('unproven OSC2 client-to-frame transform: need three independent marks')
    best = max(candidates.values())
    winners = [shift for shift, score in candidates.items() if score == best]
    if len(winners) != 1:
        raise ValueError('ambiguous OSC2 client-to-frame transform')
    dx, dy = winners[0]
    if int(dx) != dx or int(dy) != dy:
        raise ValueError('nonintegral OSC2 transform')
    return dict(dx=int(dx), dy=int(dy), source='vision_marks', support=best[0], residual=-best[1])


# @anchor:wa.perceive.osc2.join_osc2
def join_osc2(frame, description, cache):
    """Enrich each current lane; cache only a verified episode geometry transform."""
    lane = frame.get('lane', {})
    nodes = lane.get('state', {}).get('nodes', [])
    if not any(_box(n.get('box')) for n in nodes):
        return
    by_id = {n['id']: n for n in nodes}
    if len(by_id) != len(nodes):
        raise ValueError('duplicate OSC2 node id')
    key = (frame['generation'], frame['width'], frame['height'])
    if key not in cache:
        cache[key] = _estimate(nodes, description.get('marks', []))
    transform = cache[key]
    frame['osc2_transform'] = dict(transform)
    enriched = []
    for original in lane['elements']:
        node = by_id.get(original.get('state_node_id'))
        if node is None:
            enriched.append(original)
            continue
        ancestors, parent = set(), node.get('parent')
        hidden = False
        while parent is not None:
            if parent in ancestors or parent not in by_id:
                raise ValueError('invalid OSC2 ancestry')
            ancestors.add(parent)
            ancestor = by_id[parent]
            if ancestor.get('states', {}).get('expanded') in (False, 'false'):
                hidden = True
            parent = ancestor.get('parent')
        states = {k: True if v == 'true' else False if v == 'false' else v
                  for k, v in node.get('states', {}).items()
                  if k in ('enabled', 'visible', 'expanded', 'checked', 'selected',
                           'disabled', 'hidden', 'focused', 'pressed', 'busy')
                  and (type(v) is bool or v in ('true', 'false'))}
        if hidden or states.get('visible') is False:
            continue
        element = dict(original, name=node.get('name') or original.get('name', ''),
                       role=node.get('role', original.get('role', '')), states=states,
                       actionable=node.get('actionable') is True and original.get('actionable') is not False
                       and states.get('enabled') is not False,
                       parent=node.get('parent'))
        box = _box(node.get('box'))
        element['bbox'] = None
        if box and element['actionable']:
            x, y, w, h = box
            rect = [x+transform['dx'], y+transform['dy'], w, h]
            if any(int(v) != v for v in rect):
                raise ValueError('nonintegral OSC2 box')
            x, y, w, h = map(int, rect)
            if 0 <= x and 0 <= y and x+w <= frame['width'] and y+h <= frame['height']:
                element['bbox'] = dict(x=x, y=y, w=w, h=h)
        enriched.append(element)
    lane['elements'] = enriched

