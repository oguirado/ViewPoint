"""Obligation checks compare retained records and decoded pixels, not just hashes.

State paths use dots (qpos.7) or JSON Pointer (/objects/Cube.001 Big/location_um). A listed change
path covers its descendants. Other changes require allow_changes paths.
no_change covers state and decoded RGB and requires available captures.
state_equals requires exact int leaves. region is [x, y, width, height].
"""
from blender_visual_qa.drive.png import decode
from .canon import canonical_json, integer
from .contract import AdapterNotImplemented, Observation
from .receipt import make_receipt
from .verdict import PASS, FAIL, CANNOT_SEE, ERROR


def path_parts(path):
    if type(path) is not str or not path:
        raise ValueError("state_path_required")
    if path.startswith("/"):
        return tuple(p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/"))
    return tuple(path.split("."))


def lookup(state, path):
    if type(path) is not str or not path:
        raise ValueError("state_path_required")
    value = state
    for key in path_parts(path):
        if type(value) is list:
            if not key or any(c not in "0123456789" for c in key) or (len(key) > 1 and key[0] == "0"):
                raise ValueError("noncanonical_list_index:" + key)
            value = value[int(key)]
        else:
            value = value[key]
    return value


def leaves(value, prefix=()):
    if type(value) in (dict, list) and value:
        pairs = value.items() if type(value) is dict else enumerate(value)
        result = {}
        for key, item in pairs:
            result.update(leaves(item, prefix + (str(key),)))
        return result
    return {prefix: value}


def _paths(value, field):
    if type(value) is not list or not all(type(p) is str and p for p in value):
        raise ValueError("invalid_paths:" + field)
    return value


def validate_obligation(obligation):
    canonical_json(obligation)
    if type(obligation) is not dict:
        raise ValueError("obligation_must_be_object")
    kind = obligation.get("kind")
    allowed = {"state_change": {"kind", "keys", "allow_changes"},
               "state_equals": {"kind", "expected"},
               "frame_change": {"kind", "region"}, "no_change": {"kind"}}
    if type(kind) is not str or kind not in allowed:
        raise ValueError("unknown_obligation")
    if set(obligation) - allowed[kind]:
        raise ValueError("unknown_obligation_keys:" + ",".join(sorted(set(obligation) - allowed[kind])))
    if kind == "state_change":
        if not _paths(obligation.get("keys"), "keys"):
            raise ValueError("change_keys_required")
        _paths(obligation.get("allow_changes", []), "allow_changes")
    elif kind == "state_equals":
        expected = obligation.get("expected")
        if type(expected) is not dict or not expected:
            raise ValueError("expected_values_required")
        for key, value in expected.items():
            _paths([key], "expected")
            integer(value)
    elif kind == "frame_change":
        if "region" in obligation:
            region = obligation["region"]
            if type(region) is not list or len(region) != 4:
                raise ValueError("invalid_region")
            x, y, w, h = [integer(v) for v in region]
            if min(x, y) < 0 or min(w, h) <= 0:
                raise ValueError("invalid_region")
    elif kind != "no_change":
        raise ValueError("unknown_obligation")
    return obligation


def evaluate(before, after, obligation):
    kind = obligation["kind"]
    if kind == "state_change":
        required = obligation["keys"]
        try:
            unchanged = [p for p in required if lookup(before.state, p) == lookup(after.state, p)]
        except (KeyError, IndexError, TypeError, ValueError):
            return FAIL, "required_state_key_missing"
        b, a = leaves(before.state), leaves(after.state)
        permitted = [path_parts(p) for p in required + obligation.get("allow_changes", [])]
        unexpected = [p for p in sorted(b.keys() | a.keys())
                      if (p not in b or p not in a or b[p] != a[p]) and
                      not any(p[:len(k)] == k for k in permitted)]
        reasons = []
        if unchanged:
            reasons.append("required_state_unchanged:" + ",".join(unchanged))
        if unexpected:
            reasons.append("unexpected_state_change:" + ",".join("/" + "/".join(p) for p in unexpected))
        return (FAIL, ";".join(reasons)) if reasons else (PASS, "required_state_changed")
    if kind == "state_equals":
        try:
            for key, expected in obligation["expected"].items():
                actual = lookup(after.state, key)
                if type(actual) is not int or actual != expected:
                    return FAIL, "state_not_equal:" + key
        except (KeyError, IndexError, TypeError, ValueError):
            return FAIL, "expected_state_key_missing"
        return PASS, "expected_state_matches"
    if kind == "no_change" and before.state != after.state:
        return FAIL, "unexpected_state_change"
    if not before.capture_available or not after.capture_available:
        return CANNOT_SEE, "capture_unavailable"
    bw, bh, brgb = decode(before.frame_png)
    aw, ah, argb = decode(after.frame_png)
    if kind == "no_change":
        if before.state != after.state or (bw, bh, brgb) != (aw, ah, argb):
            return FAIL, "unexpected_change"
        return PASS, "state_and_frame_unchanged"
    if (bw, bh) != (aw, ah):
        return FAIL, "frame_dimensions_changed"
    if "region" in obligation:
        x, y, w, h = obligation["region"]
        if x + w > bw or y + h > bh:
            raise ValueError("region_outside_frame")
        brgb = b"".join(brgb[((y+j)*bw+x)*3:((y+j)*bw+x+w)*3] for j in range(h))
        argb = b"".join(argb[((y+j)*aw+x)*3:((y+j)*aw+x+w)*3] for j in range(h))
    return (PASS, "decoded_rgb_changed") if brgb != argb else (FAIL, "decoded_rgb_unchanged")


def verify_step(adapter, action, obligation):
    before = after = None
    checked_obligation = {"kind": "invalid"}
    rows = []
    try:
        checked_obligation = validate_obligation(obligation)
        canonical_json(action)
        if adapter.status == "not_implemented":
            raise AdapterNotImplemented("adapter_not_implemented")
        before = adapter.capture("before")
        if not isinstance(before, Observation):
            raise ValueError("capture_must_return_observation")
        before.validate(adapter.id, "before")
        paths = (checked_obligation.get("keys", []) + checked_obligation.get("allow_changes", []) +
                 list(checked_obligation.get("expected", {})))
        for path in paths:
            try:
                lookup(before.state, path)
            except (KeyError, IndexError, TypeError):
                pass  # Missing keys remain obligation failures, not invalid indices.
        result = adapter.act(action)
        if type(result) is not dict or type(result.get("ok")) is not bool:
            raise ValueError("action_result_requires_boolean_ok")
        after = adapter.capture("after")
        if not isinstance(after, Observation):
            raise ValueError("capture_must_return_observation")
        after.validate(adapter.id, "after")
        if not result["ok"]:
            rows.append({"oracle": "action", "verdict": FAIL, "reason": "action_reported_failure"})
        verdict, reason = evaluate(before, after, checked_obligation)
        rows.append({"oracle": checked_obligation["kind"], "verdict": verdict, "reason": reason})
    except AdapterNotImplemented:
        rows.append({"oracle": "adapter", "verdict": CANNOT_SEE, "reason": "adapter_not_implemented"})
    except Exception as exc:
        # Discard unvalidated observations, retaining only validated references.
        for name, obs, tag in (("before", before, "before"), ("after", after, "after")):
            try:
                obs.validate(adapter.id, tag)
            except Exception:
                if name == "before":
                    before = None
                else:
                    after = None
        rows.append({"oracle": "adapter_or_obligation", "verdict": ERROR,
                     "reason": type(exc).__name__ + ":" + str(exc)})
    return make_receipt(adapter, checked_obligation, before, after, rows)
