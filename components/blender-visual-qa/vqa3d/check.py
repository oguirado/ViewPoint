"""Obligation checks compare retained records and decoded pixels, not just hashes.

State paths use dots (qpos.7) or JSON Pointer (/objects/Cube.001 Big/location_um). A listed change
path covers its descendants. Other changes require allow_changes paths.
no_change covers state and decoded RGB and requires available captures.
state_equals requires exact int leaves. region is [x, y, width, height].
"""
from blender_visual_qa.drive.png import decode
from .canon import canonical_json, integer, sha256_canonical
from .contract import AdapterNotImplemented, Observation
from .receipt import make_receipt as _make_receipt
from .verdict import max_precedence, PASS, FAIL, CANNOT_SEE, ERROR
from .metrics import uniform_ppm, changed_ppm, noise_ppm
from .temporal import frames_from_spec, vector, evaluate_temporal, capability_check, validate_sample_request



def make_receipt(adapter, obligation, before, after, verdicts):
    """Seal capture configuration without changing the shared receipt module."""
    import json
    receipt = _make_receipt(adapter, obligation, before, after, verdicts)
    settings = {}
    for tag, observation in (("before", before), ("after", after)):
        if observation is None:
            continue
        for note in observation.notes:
            try:
                metadata = json.loads(note)
            except (ValueError, TypeError):
                continue
            if type(metadata) is dict and "capture_engine" in metadata and "capture_samples" in metadata:
                settings[tag] = {key: metadata[key] for key in ("capture_engine", "capture_samples")}
                break
    if settings:
        receipt["capture_settings"] = settings
        receipt.pop("receipt_sha256")
        receipt["receipt_sha256"] = sha256_canonical(receipt)
    return receipt


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


def change_evidence(before, after, paths):
    """Retain each changed leaf, including additions and removals, in diagnostics."""
    reasons = []
    for parts in paths:
        path = "/" + "/".join(p.replace("~", "~0").replace("/", "~1") for p in parts)
        expected = canonical_json(before[parts]) if parts in before else "AUSENTE"
        actual = canonical_json(after[parts]) if parts in after else "AUSENTE"
        reasons.append(f"unexpected_state_change:{path}:expected={expected}:actual={actual}")
    return ";".join(reasons)


def derived_permissions(obligation, paths):
    if not obligation.get("allow_derived", False):
        return []
    objects = {parts[1] for key in obligation.get("keys", [])
               for parts in [path_parts(key)] if len(parts) >= 2 and parts[0] == "objects"}
    return [p for p in paths if objects and (p == ("frame_current",) or
            len(p) >= 3 and p[0] == "objects" and p[1] in objects and
            (p[2].startswith(("world_", "view_")) or p[2] in {"bbox", "vertex_groups"}))]


def animated_permissions(obligation, before, after, b, a):
    """Only stable animation declarations across different frames imply deltas."""
    if not obligation.get("allow_animated", False):
        return []
    start, end = before.get("frame_current"), after.get("frame_current")
    paths = before.get("animation_paths")
    if (type(start) is not int or type(end) is not int or start == end or
            type(paths) is not list or paths != after.get("animation_paths") or
            any(type(p) is not str or not p.startswith("/objects/") for p in paths)):
        return []
    prefixes = [path_parts(p) for p in paths]
    return [p for p in b.keys() & a.keys() if p == ("frame_current",) or
            any(p[:len(k)] == k for k in prefixes)]


def validate_obligation(obligation):
    canonical_json(obligation)
    if type(obligation) is not dict:
        raise ValueError("obligation_must_be_object")
    kind = obligation.get("kind")
    allowed = {"state_change": {"kind", "keys", "allow_changes", "allow_derived", "allow_animated"},
               "state_equals": {"kind", "expected", "tolerance", "tolerances"},
               "within_distance": {"kind", "a", "b", "max_um", "frames", "max_sample_frames"},
               "range_over_frames": {"kind", "frames", "path", "min", "max", "max_sample_frames"},
               "max_delta_over_frames": {"kind", "frames", "path", "max_delta", "max_sample_frames"},
               "camera_is": {"kind", "name"},
               "frame_not_blank": {"kind", "max_uniform_ppm"},
               "frame_noise_below": {"kind", "max_outlier_ppm", "threshold", "luma_weights"},
               "capability": {"kind", "engine", "device_type", "denoiser", "view_transform", "look"},
               "frame_change": {"kind", "region", "pixel_tolerance", "min_changed_ppm"}, "no_change": {"kind", "keys", "allow_derived", "allow_animated"}}
    allowed["feet_floor"] = {"kind", "frames", "foot_regex", "floor_regex", "min_clearance_um", "floor"}
    allowed["foot_slide"] = {"kind", "frames", "foot_regex", "floor_regex", "plant_max_um", "max_slide_um", "plant_max_speed_um_per_frame", "floor"}
    if type(kind) is not str or kind not in allowed:
        raise ValueError("unknown_obligation")
    if set(obligation) - allowed[kind]:
        raise ValueError("unknown_obligation_keys:" + ",".join(sorted(set(obligation) - allowed[kind])))
    if kind in {"feet_floor", "foot_slide"}:
        from .profile import _params
        frames_from_spec(obligation.get("frames"))
        _params(kind, {k:v for k,v in obligation.items() if k not in {"kind", "frames"}})
    for option in ("allow_derived", "allow_animated"):
        if option in obligation and type(obligation[option]) is not bool:
            raise ValueError(option + "_requires_boolean")
    if kind == "no_change" and "keys" in obligation:
        _paths(obligation["keys"], "keys")
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
        if "tolerance" in obligation and "tolerances" in obligation:
            raise ValueError("choose_tolerance_or_tolerances")
        if "tolerances" in obligation:
            tolerances = obligation["tolerances"]
            if type(tolerances) is not dict or set(tolerances) - set(expected):
                raise ValueError("invalid_tolerances")
            for value in tolerances.values():
                if integer(value) < 0:
                    raise ValueError("negative_tolerance")
    elif kind == "within_distance":
        for key in ("a", "b"):
            value = obligation.get(key)
            if type(value) is str:
                _paths([value], key)
            else:
                vector(value)
        if integer(obligation.get("max_um")) < 0:
            raise ValueError("negative_max_um")
    elif kind in ("range_over_frames", "max_delta_over_frames"):
        _paths([obligation.get("path")], "path")
        frames_from_spec(obligation.get("frames"), obligation.get("max_sample_frames", 2000))
        if kind == "range_over_frames":
            if not {"min", "max"} & set(obligation):
                raise ValueError("range_bound_required")
            for key in ("min", "max"):
                if key in obligation:
                    integer(obligation[key])
            if "min" in obligation and "max" in obligation and obligation["min"] > obligation["max"]:
                raise ValueError("reversed_bounds")
        elif integer(obligation.get("max_delta")) < 0:
            raise ValueError("negative_max_delta")
    elif kind == "camera_is":
        _paths([obligation.get("name")], "name")
    elif kind == "capability":
        if len(obligation) == 1:
            raise ValueError("capability_requirement_required")
        for key in set(obligation) - {"kind"}:
            _paths([obligation[key]], key)
    elif kind == "frame_noise_below":
        integer(obligation.get("max_outlier_ppm"))
        weights = obligation.get("luma_weights", [77, 150, 29])
        if type(weights) is not list or len(weights) != 3 or any(integer(w) < 0 for w in weights) or sum(weights) <= 0:
            raise ValueError("invalid_luma_weights")
    elif kind == "frame_change":
        if "region" in obligation:
            region = obligation["region"]
            if type(region) is not list or len(region) != 4:
                raise ValueError("invalid_region")
            x, y, w, h = [integer(v) for v in region]
            if min(x, y) < 0 or min(w, h) <= 0:
                raise ValueError("invalid_region")
    elif kind not in ("no_change", "frame_not_blank", "feet_floor", "foot_slide"):
        raise ValueError("unknown_obligation")
    for key, maximum in (("tolerance", None), ("pixel_tolerance", 255),
                         ("min_changed_ppm", 1000000), ("max_uniform_ppm", 1000000),
                         ("max_outlier_ppm", 1000000), ("threshold", 255)):
        if key in obligation:
            value = integer(obligation[key])
            if value < 0 or (maximum is not None and value > maximum):
                raise ValueError("invalid_" + key)
    if "frames" in obligation:
        if kind == "within_distance":
            validate_sample_request(obligation["frames"], [], obligation.get("max_sample_frames", 2000), require_paths=False)
        else:
            frames_from_spec(obligation["frames"], obligation.get("max_sample_frames", 2000))
    return obligation


def evaluate(before, after, obligation):
    kind = obligation["kind"]
    if kind == "state_change":
        required = obligation["keys"]
        unchanged = []
        for path in required:
            try:
                if lookup(before.state, path) == lookup(after.state, path):
                    unchanged.append(path)
            except (KeyError, IndexError, TypeError, ValueError):
                return FAIL, "required_state_key_missing:" + path + ":value=AUSENTE"
        b, a = leaves(before.state), leaves(after.state)
        permitted = [path_parts(p) for p in required + obligation.get("allow_changes", [])]
        permitted += derived_permissions(obligation, b.keys() | a.keys())
        permitted += animated_permissions(obligation, before.state, after.state, b, a)
        unexpected = [p for p in sorted(b.keys() | a.keys())
                      if (p not in b or p not in a or b[p] != a[p]) and
                      not any(p[:len(k)] == k for k in permitted)]
        reasons = []
        if unchanged:
            for path in unchanged:
                for parts, value in leaves(lookup(after.state, path)).items():
                    suffix = ("/" if path.startswith("/") else ".").join(parts)
                    full = path + (("/" if path.startswith("/") else ".") + suffix if suffix else "")
                    reasons.append(f"required_state_unchanged:{full}:value={value}")
        if unexpected:
            reasons.append(change_evidence(b, a, unexpected))
        return (FAIL, ";".join(reasons)) if reasons else (PASS, "required_state_changed")
    if kind == "state_equals":
        try:
            for key, expected in obligation["expected"].items():
                actual = lookup(after.state, key)
                if type(actual) is not int:
                    return FAIL, f"state_not_integer:{key}:actual={actual}"
                tolerance = obligation.get("tolerances", {}).get(key, obligation.get("tolerance", 0))
                if abs(actual - expected) > tolerance:
                    return FAIL, f"state_not_equal:{key}:expected={expected}:actual={actual}"
        except (KeyError, IndexError, TypeError, ValueError):
            return FAIL, f"expected_state_key_missing:{key}:expected={expected}:actual=AUSENTE"
        return PASS, "expected_state_matches"
    if kind == "camera_is":
        if after.camera_source != "scene" or after.camera_name != obligation["name"]:
            return FAIL, f"camera_not_equal:expected={obligation['name']}:actual={after.camera_name}:source={after.camera_source}"
        return PASS, "scene_camera_matches"
    if kind in ("frame_not_blank", "frame_noise_below"):
        if not after.capture_available:
            return CANNOT_SEE, "capture_unavailable"
        width, height, rgb = decode(after.frame_png)
        if kind == "frame_not_blank":
            score = uniform_ppm(rgb)
            limit = obligation.get("max_uniform_ppm", 995000)
            return (FAIL if score >= limit else PASS), f"frame_uniform_ppm={score}:max_uniform_ppm={limit}"
        score = noise_ppm(width, height, rgb, obligation.get("threshold", 32), obligation.get("luma_weights", [77, 150, 29]))
        limit = obligation["max_outlier_ppm"]
        return (FAIL if score > limit else PASS), f"frame_outlier_ppm={score}:max_outlier_ppm={limit}"
    if kind == "no_change" and before.state != after.state:
        b, a = leaves(before.state), leaves(after.state)
        permitted = derived_permissions(obligation, b.keys() | a.keys())
        permitted += animated_permissions(obligation, before.state, after.state, b, a)
        changed = [p for p in sorted(b.keys() | a.keys()) if (p not in b or p not in a or b[p] != a[p]) and p not in permitted]
        if changed:
            return FAIL, change_evidence(b, a, changed)
    if not before.capture_available or not after.capture_available:
        return CANNOT_SEE, "capture_unavailable"
    bw, bh, brgb = decode(before.frame_png)
    aw, ah, argb = decode(after.frame_png)
    if kind == "no_change":
        if (bw, bh, brgb) != (aw, ah, argb):
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
    if "pixel_tolerance" in obligation or "min_changed_ppm" in obligation:
        score = changed_ppm(brgb, argb, obligation.get("pixel_tolerance", 0))
        limit = obligation.get("min_changed_ppm", 1)
        return (PASS if score > 0 and score >= limit else FAIL), f"changed_ppm={score}:min_changed_ppm={limit}"
    return (PASS, "decoded_rgb_changed") if brgb != argb else (FAIL, "decoded_rgb_unchanged")


def contact_obligation(adapter, obligation):
    from . import profile as policy
    raw = getattr(adapter, "wrapped", adapter)
    from .fakes import ReplayAdapter
    if not hasattr(raw, "contact_feet") and not isinstance(raw, ReplayAdapter):
        return CANNOT_SEE, "floor_unresolved:contact_sampling_unavailable", []
    profile = policy.load("film-animation")
    kind = obligation["kind"]
    params = policy.apply_defaults({k:v for k,v in obligation.items() if k != "frames"}, profile)
    state = adapter.scene_state()
    state["contact_sampling"] = 1
    raw.contact_feet = policy.resolve_feet(state.get("objects", {}), params["foot_regex"])
    raw.floor_declaration = params.get("floor")
    declaration = raw.floor_declaration or {}
    raw.contact_floor_paths = [policy.pointer("objects", name) for name,row in state.get("objects", {}).items()
        if row.get("role") == "floor" or declaration.get("object") == name or declaration.get("collection") in row.get("collections", [])]
    template = {"id": kind.replace("_", "-"), "severity": "gate", "why": "Declared scenario contact", "frames": obligation["frames"]}
    steps = policy._feet_steps(profile, template, params, state)
    verdicts, reasons, floors = [], [], []
    for step in steps:
        try:
            if step.get("cannot_see"):
                raise policy.CannotSee(step["cannot_see"])
            verdict, reason = (policy._feet_floor if kind == "feet_floor" else policy._foot_slide)(adapter, step["obligation"])
        except policy.CannotSee as exc:
            verdict, reason = CANNOT_SEE, str(exc)
        verdicts.append(verdict); reasons.append(reason)
        floors.extend({"foot": step["obligation"].get("foot"), **row} for row in step["obligation"].get("floors_used", []))
    return max_precedence(verdicts), ";".join(reasons), floors


def verify_step(adapter, action, obligation):
    before = after = None
    floors_used = None
    checked_obligation = {"kind": "invalid"}
    rows = []
    try:
        checked_obligation = validate_obligation(obligation)
        canonical_json(action)
        if adapter.status == "not_implemented":
            raise AdapterNotImplemented("adapter_not_implemented")
        if checked_obligation["kind"] == "capability":
            verdict, reason = capability_check(adapter.probe(render=True), checked_obligation)
            return make_receipt(adapter, checked_obligation, None, None,
                                [{"oracle": "capability", "verdict": verdict, "reason": reason}])
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
        kind = checked_obligation["kind"]
        if kind in {"feet_floor", "foot_slide"}:
            verdict, reason, floors_used = contact_obligation(adapter, checked_obligation)
        elif kind in ("within_distance", "range_over_frames", "max_delta_over_frames"):
            try:
                verdict, reason = evaluate_temporal(adapter, after.state, checked_obligation)
            except (KeyError, IndexError):
                verdict, reason = FAIL, "sample_state_key_missing"
        elif kind == "capability":
            verdict, reason = PASS, "capability_available"
        else:
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
        reason = str(exc)
        typed = reason.startswith(("driver_killed:", "driver_timeout:", "live_target_dead", "bridge_no_answer", "eevee_probe_timeout"))
        rows.append({"oracle": "adapter_or_obligation", "verdict": CANNOT_SEE if reason.startswith(("eevee_probe_timeout", "eevee_unavailable")) else ERROR,
                     "reason": reason if typed else type(exc).__name__ + ":" + reason})
    receipt = make_receipt(adapter, checked_obligation, before, after, rows)
    if floors_used is not None:
        receipt["floors_used"] = floors_used
        receipt["receipt_sha256"] = sha256_canonical({k:v for k,v in receipt.items() if k != "receipt_sha256"})
    return receipt
