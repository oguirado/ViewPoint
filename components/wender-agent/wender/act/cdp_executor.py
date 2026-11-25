# @module {"id":"wa.act.cdp_executor","layer":2,"owner":"ACT","deps":["wa.act.hosted_guard","wa.perceive.ax","wa.perceive.cdp_pipe","wa.state.schema"],"book":"HB-WA-s2-act"}
# @layer:2
"""CDP action executor: AX/DOM actions with post-state verification and coordinate fallback.

Preference order is API -> AX/DOM -> coordinates. Every action verifies its expected
post-state by re-reading the pruned AX tree. Irreversible actions are gated by
`ax.match` + `ax.level_allows_irreversible` (L0/L1 unique match only) and never fall
back to raw coordinates.
"""
import copy
import json
import time
from wender.act import hosted_guard
from wender.perceive import ax
from wender.perceive.cdp_pipe import CdpError
from wender.state import schema

OPS = ("click", "focus", "type", "select", "press", "insert_text", "hover", "lpath_click")

KEYS = {
    "Enter": ("Enter", 13),
    "Escape": ("Escape", 27),
    "Tab": ("Tab", 9),
    "Backspace": ("Backspace", 8),
    "ArrowLeft": ("ArrowLeft", 37),
    "ArrowUp": ("ArrowUp", 38),
    "ArrowRight": ("ArrowRight", 39),
    "ArrowDown": ("ArrowDown", 40),
}

EDITABLE_ROLES = ("textbox", "searchbox", "combobox")

JS_BY_OP = {
    "click": "function() { this.click(); return true; }",
    "focus": "function() { this.focus(); return true; }",
    "type": (
        "function(val) { this.focus(); this.value = val;"
        " this.dispatchEvent(new Event('input', {bubbles: true}));"
        " this.dispatchEvent(new Event('change', {bubbles: true}));"
        " return true; }"
    ),
    "select": (
        "function(val) { this.value = val;"
        " this.dispatchEvent(new Event('change', {bubbles: true}));"
        " return true; }"
    ),
}


# @anchor:wa.act.cdp_executor.ActionError
class ActionError(hosted_guard.ActuationError):
    """Raised when an action input is invalid or an irreversible action is refused."""
hosted_guard.bind_action_error(ActionError)

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _node_by_id(nodes, node_id):
    if not isinstance(nodes, (list, tuple)):
        return None
    for n in nodes:
        if isinstance(n, dict) and n.get("id") == node_id:
            return n
    return None


def _node_states(node):
    attrs = node.get("attrs", ())
    if isinstance(attrs, dict):
        return {str(k): str(v) for k, v in attrs.items()}
    out = {}
    if isinstance(attrs, (list, tuple)):
        for item in attrs:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out[str(item[0])] = str(item[1])
    return out


def _validate_inputs(nodes, action, read_nodes, retries, wait_ms, sleep, allow_coordinates):
    if not isinstance(nodes, (list, tuple)):
        raise ActionError("nodes must be a list or tuple")
    if not isinstance(action, dict):
        raise ActionError("action must be a dict")
    op = action.get("op")
    if op not in OPS:
        raise ActionError("unsupported op: %r" % (op,))
    target = action.get("target")
    if not isinstance(target, str) or not target:
        raise ActionError("action target must be a non-empty string")
    if op in ("type", "insert_text") and not isinstance(action.get("text"), str):
        raise ActionError("%s action requires string text" % (op,))
    if op == "select" and not isinstance(action.get("value"), str):
        raise ActionError("select action requires string value")
    if op == "press":
        key = action.get("key")
        if not isinstance(key, str) or key not in KEYS:
            raise ActionError("press action requires valid key in KEYS, got %r" % (key,))
        modifiers = action.get("modifiers", 0)
        if not _is_int(modifiers) or modifiers < 0 or modifiers > 15:
            raise ActionError("press modifiers must be int in [0, 15], got %r" % (modifiers,))
        profile = action.get("profile", "nontext")
        if not isinstance(profile, str) or profile not in ("nontext", "enter_text"):
            raise ActionError("press profile must be 'nontext' or 'enter_text', got %r" % (profile,))
        if profile == "enter_text" and key != "Enter":
            raise ActionError("enter_text profile is only valid for Enter")
        if "text" in action and action["text"] is not None:
            raise ActionError("press action does not accept text parameter")
    if "irreversible" in action and not isinstance(action["irreversible"], bool):
        raise ActionError("irreversible must be a bool")
    if op in ("hover", "lpath_click"):
        if bool(action.get("irreversible", False)):
            raise ActionError("%s cannot be irreversible" % (op,))
        if op == "hover" and action.get("origin") is not None:
            raise ActionError("hover does not accept origin")
        if op == "lpath_click":
            origin = action.get("origin")
            if (not isinstance(origin, (list, tuple)) or len(origin) != 2
                    or not _is_int(origin[0]) or not _is_int(origin[1])):
                raise ActionError("lpath_click requires integer [ox, oy] origin")
            wp = action.get("waypoint_policy", "horizontal_first")
            if not isinstance(wp, str) or wp not in ("horizontal_first", "vertical_first"):
                raise ActionError("invalid waypoint_policy: %r" % (wp,))
    if "viewport" in action and action["viewport"] is not None:
        vp = action["viewport"]
        if (not isinstance(vp, (list, tuple)) or len(vp) != 2
                or not _is_int(vp[0]) or not _is_int(vp[1]) or vp[0] <= 0 or vp[1] <= 0):
            raise ActionError("invalid viewport: %r" % (vp,))
    if not isinstance(allow_coordinates, bool):
        raise ActionError("allow_coordinates must be a bool")
    if not _is_int(retries) or retries < 1:
        raise ActionError("retries must be an int >= 1")
    if not _is_int(wait_ms) or wait_ms < 0:
        raise ActionError("wait_ms must be an int >= 0")
    if not callable(read_nodes):
        raise ActionError("read_nodes must be callable")
    if not callable(sleep):
        raise ActionError("sleep must be callable")
    expect = action.get("expect")
    if expect is not None:
        if not isinstance(expect, dict) or not expect:
            raise ActionError("expect must be a non-empty dict or None")
        for k in expect:
            if k not in ("target_states", "target_name"):
                raise ActionError("unknown expect key: %r" % (k,))
        states = expect.get("target_states")
        if states is not None:
            if not isinstance(states, dict) or not states:
                raise ActionError("expect.target_states must be a non-empty dict")
            for sk, sv in states.items():
                if not isinstance(sk, str) or not isinstance(sv, str):
                    raise ActionError("expect.target_states keys and values must be strings")
        tname = expect.get("target_name")
        if tname is not None and not isinstance(tname, str):
            raise ActionError("expect.target_name must be a string")


def _verify_expect(fresh_nodes, target_node, expect):
    node = _node_by_id(fresh_nodes, target_node["id"])
    if node is None and isinstance(expect, dict):
        expected_name = expect.get("target_name")
        if isinstance(expected_name, str):
            probe = dict(target_node, name=ax.normalize_name(expected_name))
            node = _node_by_id(fresh_nodes, ax.stable_id(probe))
        if node is None and target_node.get("backend_id") is not None:
            for cand in fresh_nodes:
                if (isinstance(cand, dict)
                        and cand.get("backend_id") == target_node.get("backend_id")
                        and cand.get("role") == target_node.get("role")):
                    node = cand
                    break
    if node is None:
        return False
    if expect is None:
        return True
    expected_name = expect.get("target_name")
    if expected_name is not None:
        if ax.normalize_name(node.get("name", "")) != ax.normalize_name(expected_name):
            return False
    expected_states = expect.get("target_states")
    if expected_states:
        actual_states = _node_states(node)
        for sk, sv in expected_states.items():
            if actual_states.get(sk) != sv:
                return False
    return True


def _resolve_box(cdp, session_id, target_node, action):
    for cand in (action.get("box"), target_node.get("box")):
        if isinstance(cand, (list, tuple)) and len(cand) == 4 and all(_is_int(v) for v in cand):
            if cand[2] > 0 and cand[3] > 0:
                return [int(cand[0]), int(cand[1]), int(cand[2]), int(cand[3])]
    backend_id = target_node.get("backend_id")
    if not _is_int(backend_id):
        return None
    hosted_guard.assert_actuation_allowed("cdp_executor._resolve_box")
    res = cdp.send(
        "DOM.getBoxModel", {"backendNodeId": int(backend_id)},
        session_id=session_id,
    )
    if not isinstance(res, dict):
        return None
    box = res.get("box")
    if isinstance(box, (list, tuple)) and len(box) == 4 and all(_is_int(v) for v in box):
        if box[2] > 0 and box[3] > 0:
            return [int(box[0]), int(box[1]), int(box[2]), int(box[3])]
    model = res.get("model") if isinstance(res.get("model"), dict) else {}
    quad = model.get("border") or model.get("content")
    if not isinstance(quad, (list, tuple)) or len(quad) != 8:
        return None
    xs = [int(round(quad[i])) for i in (0, 2, 4, 6)]
    ys = [int(round(quad[i])) for i in (1, 3, 5, 7)]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1 - x0, y1 - y0]


def _check_viewport_point(x, y, vp):
    if vp is not None:
        vw, vh = int(vp[0]), int(vp[1])
        if not (0 <= x < vw and 0 <= y < vh):
            raise RuntimeError("point (%d, %d) lies outside viewport [0..%d, 0..%d)" % (x, y, vw, vh))


def _resolve_dom_object(cdp, session_id, target_node):
    backend_id = target_node.get("backend_id")
    if not _is_int(backend_id):
        raise ActionError("target node missing backend_id: %r" % (target_node.get("id"),))
    hosted_guard.assert_actuation_allowed("cdp_executor._resolve_dom_object")
    cdp.send(
        "DOM.scrollIntoViewIfNeeded", {"backendNodeId": int(backend_id)},
        session_id=session_id,
    )
    res = cdp.send(
        "DOM.resolveNode",
        {"backendNodeId": int(backend_id)},
        session_id=session_id,
    )
    if not isinstance(res, dict):
        raise RuntimeError("DOM.resolveNode returned non-dict")
    if "object" in res and res["object"] is None:
        raise RuntimeError("DOM.resolveNode returned null object")
    obj = res.get("object") if isinstance(res.get("object"), dict) else None
    object_id = (obj.get("objectId") if obj else None) or res.get("objectId")
    if not object_id and res == {}:
        object_id = "object-%s" % (int(backend_id),)
    if not isinstance(object_id, str) or not object_id:
        raise RuntimeError("DOM.resolveNode returned no objectId")
    return object_id


def _dispatch_ax_op(cdp, session_id, target_node, action):
    hosted_guard.assert_actuation_allowed("cdp_executor._dispatch_ax_op")
    op = action["op"]
    if op == "hover":
        box = _resolve_box(cdp, session_id, target_node, action)
        if box is None:
            raise RuntimeError("no box for hover")
        cx = int(box[0] + box[2] // 2)
        cy = int(box[1] + box[3] // 2)
        _check_viewport_point(cx, cy, action.get("viewport"))
        cdp.send(
            "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx, "y": cy},
            session_id=session_id,
        )
        return
    if op == "lpath_click":
        box = _resolve_box(cdp, session_id, target_node, action)
        if box is None:
            raise RuntimeError("no box for lpath_click")
        cx = int(box[0] + box[2] // 2)
        cy = int(box[1] + box[3] // 2)
        ox, oy = int(action["origin"][0]), int(action["origin"][1])
        vp = action.get("viewport")
        _check_viewport_point(ox, oy, vp)
        _check_viewport_point(cx, cy, vp)
        wp = action.get("waypoint_policy", "horizontal_first")
        elbow = (ox, cy) if wp == "vertical_first" else (cx, oy)
        _check_viewport_point(elbow[0], elbow[1], vp)
        waypoints = []
        for pt in (elbow, (cx, cy)):
            if not waypoints or waypoints[-1] != pt:
                waypoints.append(pt)
        for wx, wy in waypoints:
            cdp.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": wx, "y": wy},
                session_id=session_id,
            )
        cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": cx, "y": cy, "button": "left", "clickCount": 1},
            session_id=session_id,
        )
        cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": cx, "y": cy, "button": "left", "clickCount": 1},
            session_id=session_id,
        )
        return
    if op == "press":
        key = action["key"]
        code, vk = KEYS[key]
        modifiers = int(action.get("modifiers", 0))
        profile = action.get("profile", "nontext")
        down_type = "keyDown" if (key == "Enter" and profile == "enter_text") else "rawKeyDown"
        down_params = {
            "type": down_type,
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "modifiers": modifiers,
        }
        if down_type == "keyDown":
            down_params["text"] = "\r"
        up_params = {
            "type": "keyUp",
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "modifiers": modifiers,
        }
        cdp.send("Input.dispatchKeyEvent", down_params, session_id=session_id)
        cdp.send("Input.dispatchKeyEvent", up_params, session_id=session_id)
        return
    if op == "insert_text":
        object_id = _resolve_dom_object(cdp, session_id, target_node)
        call_res = cdp.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": JS_BY_OP["focus"],
                "arguments": [],
                "returnByValue": True,
            },
            session_id=session_id,
        )
        if isinstance(call_res, dict) and call_res.get("exceptionDetails"):
            text = call_res["exceptionDetails"].get("text") or "Runtime.callFunctionOn exception"
            raise RuntimeError(text)
        cdp.send(
            "Input.insertText",
            {"text": action["text"]},
            session_id=session_id,
        )
        return

    object_id = _resolve_dom_object(cdp, session_id, target_node)
    args = []
    if op == "type":
        args = [{"value": action["text"]}]
    elif op == "select":
        args = [{"value": action["value"]}]

    call_res = cdp.send(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": JS_BY_OP[op],
            "arguments": args,
            "returnByValue": True,
        },
        session_id=session_id,
    )
    if isinstance(call_res, dict) and call_res.get("exceptionDetails"):
        text = call_res["exceptionDetails"].get("text") or "Runtime.callFunctionOn exception"
        raise RuntimeError(text)


# @anchor:wa.act.cdp_executor.run_ax_action
def run_ax_action(cdp, session_id, nodes, action, read_nodes,
                  retries=3, wait_ms=250, sleep=time.sleep, allow_coordinates=False,
                  *, lease=None, focus=None, observation=None, registry=None,
                  focus_policy=None, writer_fence=None, clock=None, readiness=None):
    """Execute a legacy or TARGET-aware action and verify it against fresh AX.

    Optional readiness=(binding_name, run_token) uses a caller-installed CDP
    Runtime binding on this session. Use a fresh token per action and reserve
    binding event consumption for this wait. The app signals with a JSON
    payload {"run": run_token, "owner": 1}. This replaces sleep for an action
    with an unmet AX expectation. wait_ms still caps the post-dispatch wait.
    """
    hosted_guard.assert_actuation_allowed("cdp_executor.run_ax_action")
    if readiness is not None:
        readiness = _ReadinessWait(cdp, session_id, readiness)
    if (any(v is not None for v in
            (lease, focus, observation, registry, focus_policy, writer_fence, clock))
            or (isinstance(action, dict) and any(
                key in action for key in ("handle", "epoch", "frame_seq", "digest", "actor_lease")))):
        return _run_authorized_ax_action(
            cdp, session_id, nodes, action, read_nodes,
            retries, wait_ms, sleep, allow_coordinates,
            lease, focus, observation, registry, focus_policy, writer_fence, clock,
            readiness)
    _validate_inputs(nodes, action, read_nodes, retries, wait_ms, sleep, allow_coordinates)
    nodes, action = copy.deepcopy(nodes), copy.deepcopy(action)
    target_node = _node_by_id(nodes, action["target"])
    if target_node is None:
        raise ActionError("target node absent: %r" % (action["target"],))
    op = action["op"]
    if op == "insert_text":
        if target_node.get("role") not in EDITABLE_ROLES:
            raise ActionError("insert_text target role %r is not editable" % (target_node.get("role"),))
        if _node_states(target_node).get("disabled") == "true":
            raise ActionError("insert_text target is disabled")

    irreversible = bool(action.get("irreversible", False))
    expect = action.get("expect")
    desc = None
    if irreversible:
        desc = dict(ax.describe(target_node))
        for k in ("fp_exact", "fp_stable", "name_variants"):
            if k in target_node:
                desc[k] = target_node[k]
        if isinstance(action.get("describe"), dict):
            desc.update(action["describe"])
        if not isinstance(desc.get("fp_exact"), str) or not desc.get("fp_exact"):
            raise ActionError("irreversible action refused: missing recorded fp_exact")
        initial_match = ax.match(nodes, desc)
        if (initial_match.get("established")
                or initial_match.get("tie")
                or not ax.level_allows_irreversible(initial_match.get("level"))):
            raise ActionError(
                "irreversible action refused at match level %r" % (initial_match.get("level"),)
            )

    def _run_fresh_irreversible_gate():
        nonlocal target_node
        try:
            fresh_for_gate = read_nodes()
        except Exception as exc:
            raise ActionError("fresh snapshot unavailable before irreversible dispatch") from exc
        if not isinstance(fresh_for_gate, (list, tuple)) or len(fresh_for_gate) == 0:
            raise ActionError("empty or invalid fresh snapshot for irreversible gate")
        gate_match = ax.match(fresh_for_gate, desc)
        if (gate_match.get("established")
                or gate_match.get("tie")
                or not ax.level_allows_irreversible(gate_match.get("level"))):
            raise ActionError(
                "irreversible action refused at match level %r" % (gate_match.get("level"),)
            )
        matched_node = gate_match.get("node")
        if not isinstance(matched_node, dict) or matched_node.get("backend_id") is None:
            raise ActionError("fresh irreversible target missing backend_id")
        if _node_states(matched_node).get("disabled") == "true":
            raise ActionError("fresh irreversible target is disabled")
        target_node = matched_node

    if irreversible:
        _run_fresh_irreversible_gate()

    if target_node.get("backend_id") is None and not (
        (allow_coordinates and op == "click" and not irreversible)
        or (op in ("hover", "lpath_click") and isinstance(action.get("box") or target_node.get("box"), (list, tuple)))
        or op == "press"
    ):
        raise ActionError("target node missing backend_id: %r" % (action["target"],))

    pre_sha = ax.snapshot_sha(nodes, True)
    last_nodes = list(nodes)
    attempts = 0
    last_error = None
    require_change_when_no_expect = (
        expect is None
        and op == "click"
        and target_node.get("role") in ("menuitem", "menuitemcheckbox", "menuitemradio")
    )

    def _next_nodes():
        fresh = read_nodes()
        if not isinstance(fresh, (list, tuple)):
            raise RuntimeError("read_nodes returned non-sequence")
        return list(fresh)

    dispatch_cdp = cdp if readiness is None else readiness
    for _ in range(retries):
        if irreversible and attempts > 0:
            _run_fresh_irreversible_gate()
        elif attempts > 0:
            target_node = _fresh_legacy_retry_target(nodes, read_nodes, action)
        if readiness is not None:
            readiness.check_expect(target_node, expect)
            sleep = readiness.wait
        attempts += 1
        try:
            _dispatch_ax_op(dispatch_cdp, session_id, target_node, action)
            sleep(wait_ms / 1000)
            last_nodes = _next_nodes()
            post_sha = ax.snapshot_sha(last_nodes, True)
            navigated = (expect is None and _node_by_id(last_nodes, target_node["id"]) is None
                         and post_sha != pre_sha)
            verified_ok = navigated or (
                _verify_expect(last_nodes, target_node, expect)
                and (not require_change_when_no_expect or post_sha != pre_sha)
            )
            if verified_ok:
                receipt = {
                    "ok": 1,
                    "path": "ax",
                    "attempts": attempts,
                    "verified": 1,
                    "pre_sha": pre_sha,
                    "post_sha": post_sha,
                    "error": None,
                }
                schema.dumps(receipt)
                return receipt
            last_error = "expectation not satisfied"
            updated = _node_by_id(last_nodes, target_node["id"])
            if updated is not None and updated.get("backend_id") is not None:
                target_node = updated
        except Exception as exc:
            if isinstance(exc, ActionError):
                raise
            last_error = str(exc) or exc.__class__.__name__
        if readiness is not None and readiness.action_sent:
            break

    if (allow_coordinates and op == "click" and not irreversible
            and (readiness is None or not readiness.action_sent)):
        target_node = _fresh_legacy_retry_target(nodes, read_nodes, action, coordinates=True)
        if readiness is not None:
            readiness.check_expect(target_node, expect)
            sleep = readiness.wait
        attempts += 1
        try:
            box = _resolve_box(cdp, session_id, target_node, action)
            if box is None:
                raise RuntimeError("no box for coordinates fallback")
            cx = int(box[0] + box[2] // 2)
            cy = int(box[1] + box[3] // 2)
            _check_viewport_point(cx, cy, action.get("viewport"))
            cdp.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": cx, "y": cy},
                session_id=session_id,
            )
            cdp.send(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": cx, "y": cy, "button": "left", "clickCount": 1},
                session_id=session_id,
            )
            cdp.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseReleased", "x": cx, "y": cy, "button": "left", "clickCount": 1},
                session_id=session_id,
            )
            sleep(wait_ms / 1000)
            last_nodes = _next_nodes()
            post_sha = ax.snapshot_sha(last_nodes, True)
            navigated = (expect is None and _node_by_id(last_nodes, target_node["id"]) is None
                         and post_sha != pre_sha)
            if navigated or (
                _verify_expect(last_nodes, target_node, expect)
                and (not require_change_when_no_expect or post_sha != pre_sha)
            ):
                receipt = {
                    "ok": 1,
                    "path": "coordinates",
                    "attempts": attempts,
                    "verified": 1,
                    "pre_sha": pre_sha,
                    "post_sha": post_sha,
                    "error": None,
                }
                schema.dumps(receipt)
                return receipt
            last_error = "expectation not satisfied"
        except Exception as exc:
            last_error = str(exc) or exc.__class__.__name__
        receipt = {
            "ok": 0,
            "path": "coordinates",
            "attempts": attempts,
            "verified": 0,
            "pre_sha": pre_sha,
            "post_sha": ax.snapshot_sha(last_nodes, True),
            "error": last_error,
        }
        schema.dumps(receipt)
        return receipt

    receipt = {
        "ok": 0,
        "path": "ax",
        "attempts": attempts,
        "verified": 0,
        "pre_sha": pre_sha,
        "post_sha": ax.snapshot_sha(last_nodes, True),
        "error": last_error,
    }
    schema.dumps(receipt)
    return receipt


def _fresh_legacy_retry_target(nodes, read_nodes, action, *, coordinates=False):
    """Re-resolve original identity before any additional legacy dispatch."""
    # Legacy callers have no TARGET authority.  Do not invent one, or permit a
    # same-id post-state node to replace the original role/fingerprint contract.
    require_backend = not (coordinates or action["op"] in ("press", "hover", "lpath_click"))
    target, _ = _fresh_authorized_target(nodes, read_nodes, action,
                                         require_backend=require_backend)
    if action["op"] == "insert_text" and target.get("role") not in EDITABLE_ROLES:
        raise ActionError("fresh insert_text target is not editable")
    return target


def _validate_authorized_observation(registry, lease, observation, session_id):
    """Authenticate current identity/epoch/sequence and validate actual RGB bytes."""
    from wender.act.focus_policy import _same_handle, _valid_integer

    for method in ("get", "validate_observation"):
        if not callable(getattr(registry, method, None)):
            raise ActionError("session registry %s prerequisite AUSENTE" % method)
    handle = getattr(lease, "handle", None)
    if getattr(handle, "kind", None) != "cdp_tab":
        raise ActionError("CDP executor requires a cdp_tab handle")
    try:
        record = registry.get(handle)
        current = registry.validate_observation(observation)
    except Exception as exc:
        raise ActionError("current target/observation authority unavailable") from exc
    if (record is None or getattr(record, "closed", True)
            or not _same_handle(getattr(record, "handle", None), handle)
            or not _valid_integer(getattr(record, "epoch", None))
            or record.epoch < 0
            or getattr(record, "epoch", None) != getattr(observation, "epoch", None)):
        raise ActionError("missing or stale target record/geometry epoch")
    if (not isinstance(session_id, str) or not session_id
            or getattr(record, "session_id", None) != session_id):
        raise ActionError("CDP session does not own canonical target")
    if current is not True:
        raise ActionError("registry-issued current observation prerequisite AUSENTE")
    frame = getattr(observation, "frame", None)
    seq = getattr(observation, "frame_seq", None)
    if (not _same_handle(getattr(observation, "handle", None), handle)
            or not _same_handle(getattr(frame, "handle", None), handle)
            or not _valid_integer(getattr(observation, "epoch", None))
            or observation.epoch < 0
            or getattr(frame, "epoch", None) != observation.epoch
            or getattr(frame, "frame_seq", None) != seq
            or not _valid_integer(seq) or seq < 0
            or not _valid_integer(getattr(observation, "dpr_ppm", None))
            or observation.dpr_ppm <= 0
            or not _valid_integer(getattr(frame, "width", None)) or frame.width <= 0
            or not _valid_integer(getattr(frame, "height", None)) or frame.height <= 0
            or not isinstance(getattr(frame, "digest", None), str) or not frame.digest
            or not isinstance(getattr(observation, "state", None), dict)):
        raise ActionError("inconsistent frame or observation identity")
    try:
        # This merged constructor checks handle, integer fields, RGB shape and
        # exact SHA-256, and owns immutable bytes.  Never fabricate a fallback.
        from wender.perceive.session_registry import FrameRef
        FrameRef(frame.handle, frame.epoch, frame.frame_seq, frame.width,
                 frame.height, frame.rgb, frame.digest)
    except ImportError as exc:
        raise ActionError("frame validation prerequisite AUSENTE") from exc
    except Exception as exc:
        raise ActionError("invalid current frame content or digest") from exc
    state = observation.state
    if (("handle" in state and not _same_handle(state["handle"], handle))
            or ("epoch" in state and (not _valid_integer(state["epoch"])
                                     or state["epoch"] != observation.epoch))
            or ("frame_seq" in state and (not _valid_integer(state["frame_seq"])
                                         or state["frame_seq"] != seq))
            or ("digest" in state and state["digest"] != frame.digest)
            or ("frame_digest" in state and state["frame_digest"] != frame.digest)):
        raise ActionError("state aliases a sibling or stale target")
    return copy.deepcopy(state)


def _validate_authorized_action(action, lease, observation):
    """Optional request aliases may restrict authority, never replace it."""
    from wender.act.focus_policy import _same_handle, _valid_integer

    if ("handle" in action and not _same_handle(action["handle"], lease.handle)):
        raise ActionError("action handle differs from current target")
    for field in ("epoch", "frame_seq"):
        if field in action and (not _valid_integer(action[field])
                                or action[field] != getattr(observation, field)):
            raise ActionError("action %s differs from current observation" % field)
    if "digest" in action and action["digest"] != observation.frame.digest:
        raise ActionError("action digest differs from current frame")
    if "actor_lease" in action and action["actor_lease"] != lease:
        raise ActionError("action actor lease differs from admitted authority")


def _validate_authorized_target(action, target, state):
    """Bind fresh AX identity and keyboard focus to the latest admitted state."""
    observed_nodes = state.get("nodes")
    if not isinstance(observed_nodes, (list, tuple)):
        raise ActionError("current observed nodes are unavailable")
    matches = [node for node in observed_nodes
               if isinstance(node, dict) and node.get("id") == target["id"]]
    if len(matches) != 1:
        raise ActionError("current observed target is missing or ambiguous")
    observed = matches[0]
    if (not _is_int(observed.get("backend_id"))
            or observed.get("backend_id") != target["backend_id"]
            or observed.get("role") != target.get("role")):
        raise ActionError("fresh backend does not match current observed target")
    try:
        same_identity = (ax.fingerprints(observed)[1] == ax.fingerprints(target)[1]
                         and observed.get("ordinal") == target.get("ordinal"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ActionError("current observed identity is malformed") from exc
    if not same_identity or _node_states(observed).get("disabled") == "true":
        raise ActionError("current observed identity changed or is disabled")
    if (action["op"] in ("press", "insert_text", "type", "select")
            and state.get("focused_node_id") != action["target"]):
        raise ActionError("keyboard/edit requires current focused element")
    if action["op"] == "insert_text" and target.get("role") not in EDITABLE_ROLES:
        raise ActionError("insert_text target role is not editable")


def _fresh_authorized_target(nodes, read_nodes, action, *, require_backend=True):
    """Fail before all CDP mutation, including DOM.scrollIntoViewIfNeeded."""
    target = _node_by_id(nodes, action["target"])
    if target is None:
        raise ActionError("recorded target absent")
    try:
        descriptor = dict(ax.describe(target))
        if isinstance(action.get("describe"), dict):
            recorded = action["describe"]
            if recorded.get("fp_exact", descriptor["fp_exact"]) != descriptor["fp_exact"]:
                raise ActionError("caller fingerprint differs from recorded node")
        if not descriptor.get("fp_exact") or not descriptor.get("fp_stable"):
            raise ActionError("recorded target lacks fingerprints")
        first_match = ax.match(nodes, descriptor)
        if (first_match.get("tie") or first_match.get("established")
                or not ax.level_allows_irreversible(first_match.get("level"))):
            raise ActionError("initial target match is ambiguous")
        snapshot = copy.deepcopy(read_nodes())
        if not isinstance(snapshot, (list, tuple)) or not snapshot:
            raise ActionError("fresh AX snapshot is empty or invalid")
        match = ax.match(snapshot, descriptor)
        if (match.get("tie") or match.get("established")
                or not ax.level_allows_irreversible(match.get("level"))):
            raise ActionError("fresh AX target tie or identity downgrade")
        node = match.get("node")
        if (not isinstance(node, dict)
                or (require_backend and (not _is_int(node.get("backend_id"))
                                         or node["backend_id"] <= 0))
                or _node_states(node).get("disabled") == "true"):
            raise ActionError("fresh unique target lacks actionable backend")
        return node, snapshot
    except ActionError:
        raise
    except Exception as exc:
        # Includes StopIteration: never interpret unreadability as empty success.
        raise ActionError("fresh AX gate unreadable or invalid") from exc


def _authorized_input_events(action, node, state):
    """Pointer plans use only observation-owned geometry, never caller overrides."""
    from wender.act import coordinates

    obs_nodes = state.get("nodes")
    obs_node = _node_by_id(obs_nodes, node["id"])
    if (obs_node is None or obs_node.get("backend_id") != node["backend_id"]
            or obs_node.get("role") != node.get("role")):
        raise ActionError("fresh AX backend is not in same-target observation")
    box = obs_node.get("box")
    if node.get("box") is not None and box != node["box"]:
        raise ActionError("AX box differs from observation geometry")
    if action.get("box") is not None and action["box"] != box:
        raise ActionError("caller box differs from same-target geometry")
    viewport = state.get("viewport")
    if not isinstance(viewport, dict):
        raise ActionError("same-target viewport prerequisite AUSENTE")
    if action.get("viewport") is not None and list(action["viewport"]) != [viewport.get("w"), viewport.get("h")]:
        raise ActionError("caller viewport differs from same-target viewport")
    observed = {"nodes": [{"id": node["id"], "box": box}], "viewport": viewport}
    op = action["op"]
    try:
        if op == "hover":
            return coordinates.plan_hover(observed, node["id"])
        if op == "lpath_click":
            return coordinates.plan_lpath_click(
                observed, node["id"], action["origin"],
                waypoint_policy=action.get("waypoint_policy", "horizontal_first"))
        if op == "click":
            return coordinates.plan_click(observed, node["id"])
    except coordinates.CoordRefused as exc:
        raise ActionError("stale or malformed target geometry: %s" % (exc,)) from exc
    raise ActionError("unsupported coordinate operation")


def _run_authorized_ax_action(cdp, session_id, nodes, action, read_nodes,
                              retries, wait_ms, sleep, allow_coordinates,
                              lease, focus, observation, registry, focus_policy,
                              writer_fence, clock, readiness=None):
    """One-shot TARGET action behind owned lease/focus/freshness and writer fence.

    All helper dependencies are explicit.  No fallback admits a partial send.
    Caller retains responsibility for releasing/quarantining its actor lease.
    """
    hosted_guard.assert_actuation_allowed("cdp_executor._run_authorized_ax_action")
    try:
        from wender.act.focus_policy import FocusClass, FocusRefused
    except ImportError as exc:
        raise ActionError("focus authority prerequisite AUSENTE") from exc
    _validate_inputs(nodes, action, read_nodes, retries, wait_ms, sleep, allow_coordinates)
    # Reader/provider callbacks must not rewrite the validated request or its
    # recorded identity while authorization is in progress.
    nodes, action = copy.deepcopy(nodes), copy.deepcopy(action)
    if any(v is None for v in (lease, focus, observation, registry, focus_policy, writer_fence)):
        raise ActionError("target-aware action missing authority prerequisite AUSENTE")
    for method in ("acquire", "release", "quarantine"):
        if not callable(getattr(writer_fence, method, None)):
            raise ActionError("writer fence %s prerequisite AUSENTE" % method)
    for method in ("classify", "validate", "consume"):
        if not callable(getattr(focus_policy, method, None)):
            raise ActionError("focus grant %s prerequisite AUSENTE" % method)
    if clock is not None and not callable(clock):
        raise ActionError("clock must be callable")
    now_ns = time.monotonic_ns() if clock is None else clock()
    domain = "cdp:%s" % (session_id,)
    mode = "coordinates" if action.get("path") == "coordinates" else "ax"
    op = action["op"]
    category = focus_policy.classify(mode, op)
    if category == FocusClass.REFUSE:
        raise ActionError("unclassified or unsupported operation refused")
    if mode == "coordinates" and (not allow_coordinates or action.get("irreversible")):
        raise ActionError("coordinate route not authorized")
    if mode == "coordinates" and op not in ("click", "hover", "lpath_click"):
        raise ActionError("no safe coordinate route for this op")
    state = _validate_authorized_observation(registry, lease, observation, session_id)
    _validate_authorized_action(action, lease, observation)
    try:
        focus_policy.validate(lease, focus, observation, domain=domain, now_ns=now_ns)
    except FocusRefused as exc:
        raise ActionError("focus authority refused: %s" % exc) from exc
    if op in ("press", "insert_text", "type", "select"):
        if state.get("focused_node_id") != action["target"]:
            raise ActionError("keyboard/edit requires current focused element")
    acquired = False
    dispatched = False
    quarantined = False
    try:
        if writer_fence.acquire(lease) is not True:
            raise ActionError("writer busy or lease already consumed")
        acquired = True
        target, fresh_nodes = _fresh_authorized_target(nodes, read_nodes, action)
        before_sha = ax.snapshot_sha(fresh_nodes, True)
        # Validate again after fresh reads; domain focus, lease and geometry may
        # have changed while the non-mutating reader ran.
        state = _validate_authorized_observation(registry, lease, observation, session_id)
        _validate_authorized_action(action, lease, observation)
        _validate_authorized_target(action, target, state)
        if readiness is not None:
            readiness.check_expect(target, action.get("expect"))
            sleep = readiness.wait
        if mode == "coordinates" or op in ("hover", "lpath_click"):
            commands = _authorized_input_events(action, target, state)
        else:
            commands = None
        focus_policy.validate(lease, focus, observation, domain=domain,
                              now_ns=time.monotonic_ns() if clock is None else clock())
        focus_policy.consume(focus)  # spend nonce before *any* transport write
        dispatched = True  # even DOM.scrollIntoView is a mutating send
        if commands is None:
            _dispatch_ax_op(cdp, session_id, target, action)
        else:
            for params in commands:
                cdp.send("Input.dispatchMouseEvent", params, session_id=session_id)
        sleep(wait_ms / 1000)
        post = read_nodes()
        if not isinstance(post, (list, tuple)) or not post:
            raise RuntimeError("post-action AX snapshot unavailable")
        after_sha = ax.snapshot_sha(post, True)
        expect = action.get("expect")
        navigated = (expect is None and _node_by_id(post, target["id"]) is None
                     and before_sha != after_sha)
        verified = navigated or (
            _verify_expect(post, target, expect)
            and (expect is not None or before_sha != after_sha)
        )
        if not verified and action.get("irreversible", False):
            raise RuntimeError("irreversible outcome cannot be verified")
        receipt = {"ok": int(verified), "path": mode, "attempts": 1,
                   "verified": int(verified), "pre_sha": before_sha,
                   "post_sha": after_sha,
                   "error": None if verified else "postcondition not established"}
        schema.dumps(receipt)
        return receipt
    except ActionError:
        if dispatched:
            writer_fence.quarantine(lease)
            quarantined = True
        raise
    except Exception as exc:
        if dispatched:
            writer_fence.quarantine(lease)
            quarantined = True
            receipt = {"ok": 0, "path": mode, "attempts": 1,
                       "verified": 0, "pre_sha": before_sha,
                       "post_sha": None, "error": "ambiguous outcome: %s" % (exc,)}
            schema.dumps(receipt)
            return receipt
        raise ActionError("pre-dispatch gate failed: %s" % (exc,)) from exc
    finally:
        if acquired and not quarantined:
            try:
                writer_fence.release(lease)
            except Exception:
                writer_fence.quarantine(lease)
                raise


class _ReadinessWait:
    """Bound the wait and remember whether a legacy action may have been sent."""

    def __init__(self, cdp, session_id, readiness):
        if (not isinstance(readiness, tuple) or len(readiness) != 2
                or not all(isinstance(value, str) and value for value in readiness)):
            raise ActionError("readiness must be a (binding_name, run_token) tuple")
        if not isinstance(session_id, str) or not session_id:
            raise ActionError("readiness requires a CDP session")
        if not callable(getattr(cdp, "wait_event", None)):
            raise ActionError("readiness requires cdp.wait_event")
        self.cdp = cdp
        self.session_id = session_id
        self.binding_name, self.run_token = readiness
        self.action_sent = False

    def send(self, method, params, *, session_id):
        # A transport failure may leave a DOM action or native input forwarded.
        if method == "Runtime.callFunctionOn" or method.startswith("Input."):
            self.action_sent = True
        return self.cdp.send(method, params, session_id=session_id)

    def check_expect(self, target, expect):
        if expect is None or _verify_expect([target], target, expect):
            raise ActionError("readiness requires an unmet AX expectation")

    def wait(self, seconds):
        method = "Runtime.bindingCalled"
        deadline = time.monotonic() + seconds
        remaining = seconds
        while remaining > 0:
            try:
                event = self.cdp.wait_event(method, session_id=self.session_id,
                                            timeout=remaining)
            except CdpError as exc:
                if str(exc) == "timeout waiting for event " + method:
                    return
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._matches(event):
                return

    def _matches(self, event):
        params = event.get("params") if isinstance(event, dict) else None
        if not isinstance(params, dict) or params.get("name") != self.binding_name:
            return False
        raw = params.get("payload")
        if not isinstance(raw, str):
            return False
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            return False
        return (payload == {"run": self.run_token, "owner": 1}
                and _is_int(payload["owner"]))
