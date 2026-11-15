# @module {"id":"wa.act.coordinates","layer":1,"owner":"ACT","deps":["wa.state.schema"],"book":"HB-WA-s2-act"}
"""@module wa.act.coordinates - last-resort coordinates action planner.

Provides pure functions for coordinate conversions and click/type action planning
on STATE elements when structured (API/AX) paths are unavailable.
"""


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# @anchor:wa.act.coordinates.CoordRefused
class CoordRefused(ValueError):
    """Raised when a coordinate action cannot be safely planned."""
    pass


# @anchor:wa.act.coordinates.box_center
def box_center(box):
    """Return integer center (x, y) of a STATE box [x, y, w, h] using floor division."""
    if not isinstance(box, (list, tuple)) or len(box) != 4 or not all(_is_int(v) for v in box):
        raise CoordRefused("invalid box")
    x, y, w, h = box
    if w <= 0 or h <= 0:
        raise CoordRefused("invalid box")
    return x + w // 2, y + h // 2


# @anchor:wa.act.coordinates.css_to_device
def css_to_device(x, y, dpr_ppm):
    """Convert CSS px coordinates to device px using integer arithmetic with dpr_ppm."""
    if not (_is_int(x) and _is_int(y) and _is_int(dpr_ppm) and dpr_ppm > 0):
        raise CoordRefused("invalid css_to_device parameters")
    return (x * dpr_ppm) // 1000000, (y * dpr_ppm) // 1000000


def _resolve_state_node_center(state, node_id, *, irreversible=False):
    if not isinstance(irreversible, bool) or irreversible:
        raise CoordRefused("irreversible actions are refused on coordinates path")
    if not isinstance(state, dict) or not isinstance(node_id, str) or not node_id:
        raise CoordRefused("invalid state or node_id")

    nodes = state.get("nodes") or []
    node = None
    for n in nodes:
        if isinstance(n, dict) and n.get("id") == node_id:
            node = n
            break

    if node is None:
        raise CoordRefused("node %r missing in state" % (node_id,))

    box = node.get("box")
    if not isinstance(box, (list, tuple)) or len(box) != 4 or not all(_is_int(v) for v in box):
        raise CoordRefused("node %r has null or missing box" % (node_id,))

    x, y, w, h = box
    if w <= 0 or h <= 0:
        raise CoordRefused("node %r has zero-size box" % (node_id,))

    cx, cy = box_center(box)

    vp = state.get("viewport")
    if vp is not None:
        if not isinstance(vp, dict):
            raise CoordRefused("invalid viewport in state")
        vw = vp.get("w", 0)
        vh = vp.get("h", 0)
        sx = vp.get("scroll_x", 0)
        sy = vp.get("scroll_y", 0)
        if not (_is_int(vw) and _is_int(vh) and _is_int(sx) and _is_int(sy) and vw > 0 and vh > 0):
            raise CoordRefused("invalid viewport dimensions in state")

        min_x = sx
        max_x = sx + vw
        min_y = sy
        max_y = sy + vh

        if not (min_x <= cx < max_x and min_y <= cy < max_y):
            raise CoordRefused("node %r center (%d, %d) lies outside viewport [%d..%d, %d..%d]" %
                               (node_id, cx, cy, min_x, max_x, min_y, max_y))
    return cx, cy, vp


# @anchor:wa.act.coordinates.plan_click
def plan_click(state, node_id, *, irreversible=False):
    """Plan CDP mouse events to click a STATE node by id."""
    cx, cy, _ = _resolve_state_node_center(state, node_id, irreversible=irreversible)
    return [
        {"type": "mouseMoved", "x": cx, "y": cy},
        {"type": "mousePressed", "button": "left", "clickCount": 1, "x": cx, "y": cy},
        {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": cx, "y": cy},
    ]


def plan_hover(state, node_id, *, irreversible=False):
    """Plan CDP mouseMoved event to hover a STATE node by id."""
    cx, cy, _ = _resolve_state_node_center(state, node_id, irreversible=irreversible)
    return [{"type": "mouseMoved", "x": cx, "y": cy}]


def plan_lpath_click(state, node_id, origin, *, irreversible=False, waypoint_policy="horizontal_first"):
    """Plan orthogonal L-path CDP mouse events from origin to target center."""
    if (not isinstance(origin, (list, tuple)) or len(origin) != 2
            or not _is_int(origin[0]) or not _is_int(origin[1])):
        raise CoordRefused("invalid origin")
    if waypoint_policy not in ("horizontal_first", "vertical_first"):
        raise CoordRefused("invalid waypoint_policy")
    cx, cy, vp = _resolve_state_node_center(state, node_id, irreversible=irreversible)
    ox, oy = origin[0], origin[1]
    elbow = (ox, cy) if waypoint_policy == "vertical_first" else (cx, oy)
    if vp is not None:
        vw, vh = vp.get("w", 0), vp.get("h", 0)
        sx, sy = vp.get("scroll_x", 0), vp.get("scroll_y", 0)
        for px, py in ((ox, oy), elbow):
            if not (sx <= px < sx + vw and sy <= py < sy + vh):
                raise CoordRefused("waypoint (%d, %d) lies outside viewport" % (px, py))
    waypoints = []
    for pt in (elbow, (cx, cy)):
        if not waypoints or waypoints[-1] != pt:
            waypoints.append(pt)
    events = [{"type": "mouseMoved", "x": wx, "y": wy} for wx, wy in waypoints]
    events.append({"type": "mousePressed", "button": "left", "clickCount": 1, "x": cx, "y": cy})
    events.append({"type": "mouseReleased", "button": "left", "clickCount": 1, "x": cx, "y": cy})
    return events


# @anchor:wa.act.coordinates.plan_type
def plan_type(text):
    """Plan CDP Input.insertText params dict list."""
    if not isinstance(text, str):
        raise CoordRefused("text must be a string")
    return [{"type": "insertText", "text": text}]
