"""Domain profiles: strict policy data, deterministic selectors, honest observations.

Profile oracles live here; they do not extend the core obligation contract.
All policy numbers have JSON-Pointer provenance. No runtime is imported on load.
"""
import copy
import inspect
import json
import math
from pathlib import Path
import re

from .canon import canonical_json, integer, sha256_canonical, validate as validate_json
from .check import evaluate, lookup, validate_obligation, verify_step
from .contract import Observation
from .receipt import make_receipt
from .temporal import capability_check, evaluate_temporal, frames_from_spec, sample_adapter
from .verdict import max_precedence, rc_of

SCHEMA = "vqa3d.profile/v1"
DIRECTORY = Path(__file__).with_name("profiles")
TYPES = {"MESH", "ARMATURE", "LIGHT", "CAMERA", "EMPTY"}
CAP_FIELDS = {"engine", "device_type", "denoiser", "view_transform", "look"}
PARAMS = {
    "state_change": {"keys", "allow_changes"},
    "state_equals": {"expected", "tolerance", "tolerances"},
    "camera_is": {"name"},
    "range_over_frames": {"path", "min", "max"},
    "max_delta_over_frames": {"path", "max_delta"},
    "within_distance": {"a", "b", "max_um"},
    "frame_not_blank": {"max_uniform_ppm"},
    "frame_noise_below": {"max_outlier_ppm", "threshold"},
    "frame_change": {"pixel_tolerance", "min_changed_ppm", "steps", "region"},
    "no_change": set(), "capability": CAP_FIELDS,
    "scene_camera": set(), "render_settings": {"enforce"},
    "feet_floor": {"foot_regex", "floor_regex", "min_clearance_um"},
    "foot_slide": {"foot_regex", "floor_regex", "plant_max_um", "max_slide_um"},
    "bone_rotation_continuity": {"path", "max_delta_mdeg", "max_detour_mdeg"},
    "attachment": {"prop_regex", "max_um", "max_drift_um"},
    "tri_budget": {"max_object_tris", "max_scene_tris"},
    "texture_budget": {"max_texture_px"},
    "scale_range": {"min_extent_um", "max_extent_um"},
    "light_state": {"min_energy_mw"},
    "contacts": {"min_contacts", "steps"},
    "replay_identical": {"steps"}, "interpenetration": {"max_pairs"},
}
TEMPORAL = {"range_over_frames", "max_delta_over_frames", "within_distance"}
PIXELS = {"frame_not_blank", "frame_noise_below", "frame_change", "no_change"}
# Profile-local film oracles sampled over the whole shot (never core obligations).
FEET = {"feet_floor", "foot_slide"}
SHOT = FEET | {"bone_rotation_continuity", "attachment"}
REGEX_PARAMS = {"foot_regex", "floor_regex", "prop_regex"}
RENDER_FIELDS = {"engine", "samples", "view_transform", "look", "motion_blur", "resolution_pct"}
SLIDE_CANDIDATES = 8  # ankle-to-toe line sampled at k/8, k = 0..8


class CannotSee(RuntimeError):
    """A required observation or constructor interface is absent."""


def _keys(value, allowed, required=()):
    if type(value) is not dict or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError("profile_unknown_or_missing_keys")


def _line(value):
    if type(value) is not str or not value or "\n" in value or "\r" in value:
        raise ValueError("profile_one_line_required")


def _id(value):
    if type(value) is not str or re.fullmatch(r"[a-z][a-z0-9-]*", value) is None:
        raise ValueError("profile_invalid_id")


def pointer(*parts):
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def numeric_paths(value, path=""):
    if type(value) is int:
        yield path
    elif type(value) is dict:
        for key, item in value.items():
            if key != "provenance":
                yield from numeric_paths(item, path + pointer(key))
    elif type(value) is list:
        for index, item in enumerate(value):
            yield from numeric_paths(item, path + pointer(index))


def _params(kind, params):
    if type(kind) is not str or kind not in PARAMS:
        raise ValueError("profile_unknown_kind:" + str(kind))
    _keys(params, PARAMS[kind])
    for key, value in params.items():
        if key in {"path", "a", "b", "name"} | CAP_FIELDS | REGEX_PARAMS:
            _line(value)
            if key in REGEX_PARAMS:
                try:
                    re.compile(value)
                except re.error:
                    raise ValueError("profile_invalid_regex:" + key) from None
                if "{" in value:
                    raise ValueError("profile_regex_braces_reserved_for_placeholders:" + key)
        elif key == "enforce":
            if type(value) is not list or not value or len(set(value)) != len(value) or set(value) - RENDER_FIELDS:
                raise ValueError("profile_invalid_render_enforce")
        elif key in {"keys", "allow_changes"}:
            if type(value) is not list:
                raise ValueError("profile_paths_list_required")
            for path in value:
                _line(path)
        elif key in {"expected", "tolerances"}:
            if type(value) is not dict:
                raise ValueError("profile_path_values_required")
            for path, number in value.items():
                _line(path)
                integer(number)
                if key == "tolerances" and number < 0:
                    raise ValueError("profile_negative_tolerance")
        elif key == "region":
            if type(value) is not list or len(value) != 4 or any(type(v) is not int for v in value):
                raise ValueError("profile_invalid_region")
            if min(value[:2]) < 0 or min(value[2:]) <= 0:
                raise ValueError("profile_invalid_region")
        else:
            integer(value)
            if key not in {"min", "max", "min_clearance_um"} and value < 0:
                raise ValueError("profile_negative_parameter:" + key)
            if key.endswith("ppm") and not 0 <= value <= 1000000:
                raise ValueError("profile_invalid_ppm")
            if key in {"threshold", "pixel_tolerance"} and value > 255:
                raise ValueError("profile_invalid_pixel_threshold")
            if key == "steps" and not 1 <= value <= 10000:
                raise ValueError("profile_invalid_steps")


def validate(profile, *, partial=False):
    """Validate nested keys, types and provenance, including every integer leaf."""
    validate_json(profile, allow_bool=False)
    required = {"schema", "id"} if partial else {
        "schema", "id", "intent", "render", "capture", "budgets", "defaults", "requires", "checks", "provenance"}
    _keys(profile, required | {"extends", "intent", "render", "capture", "budgets", "defaults", "requires", "checks", "provenance"}, required)
    if profile["schema"] != SCHEMA:
        raise ValueError("profile_schema_mismatch")
    _id(profile["id"])
    if "extends" in profile:
        _id(profile["extends"])
    if "intent" in profile:
        _line(profile["intent"])
    for name, fields, optional in (("render", RENDER_FIELDS, {"look"}),
                                   ("capture", {"engine", "samples", "max_w", "max_h", "max_frames"}, {"max_frames"})):
        if name not in profile:
            continue
        value = profile[name]
        _keys(value, fields, () if partial else fields - optional)
        for key, item in value.items():
            if key in {"engine", "view_transform", "look"}:
                _line(item)
            else:
                integer(item)
                if key == "motion_blur":
                    if item not in (0, 1):
                        raise ValueError("profile_invalid_motion_blur")
                elif item <= 0:
                    raise ValueError("profile_positive_setting_required")
        if name == "capture" and value.get("engine") not in (None, "workbench", "eevee", "cycles", "egl"):
            raise ValueError("profile_invalid_capture_engine")
        if name == "render" and value.get("resolution_pct", 100) > 100:
            raise ValueError("profile_invalid_resolution_pct")
        if name == "capture" and (value.get("max_w", 1) > 96 or value.get("max_h", 1) > 72):
            raise ValueError("profile_capture_exceeds_adapter_contract")
    if "budgets" in profile and profile["budgets"] is not None:
        _keys(profile["budgets"], {"max_object_tris", "max_scene_tris", "max_texture_px"})
        for value in profile["budgets"].values():
            if integer(value) <= 0:
                raise ValueError("profile_positive_budget_required")
    if "defaults" in profile:
        if type(profile["defaults"]) is not dict:
            raise ValueError("profile_defaults_object_required")
        for kind, params in profile["defaults"].items():
            _params(kind, params)
    if "requires" in profile:
        _keys(profile["requires"], CAP_FIELDS)
        for value in profile["requires"].values():
            _line(value)
    if "checks" in profile:
        if type(profile["checks"]) is not list or (not partial and not profile["checks"]):
            raise ValueError("profile_nonempty_checks_required")
        seen = set()
        for check in profile["checks"]:
            _keys(check, {"id", "kind", "select", "params", "frames", "severity", "why"},
                  {"id", "kind", "select", "params", "frames", "severity", "why"})
            _id(check["id"])
            if check["id"] in seen:
                raise ValueError("profile_duplicate_check_id")
            seen.add(check["id"])
            _line(check["why"])
            _params(check["kind"], check["params"])
            select = check["select"]
            _keys(select, {"type", "name_regex", "parent_bone_regex"})
            if select and select.get("type") not in TYPES:
                raise ValueError("profile_selector_type_required")
            for key in set(select) - {"type"}:
                _line(select[key])
                re.compile(select[key])
            if check["severity"] not in {"gate", "advisory"}:
                raise ValueError("profile_invalid_severity")
            if check["frames"] != "scene":
                frames_from_spec(check["frames"])
            if not partial:
                kind = check["kind"]
                effective = {**profile.get("defaults", {}).get(kind, {}), **check["params"]}
                if kind in {"tri_budget", "texture_budget"}:
                    budget = {k: v for k, v in (profile.get("budgets") or {}).items() if k in PARAMS[kind]}
                    if any(k in effective and effective[k] != v for k, v in budget.items()):
                        raise ValueError("profile_check_conflicts_with_budget")
                    effective = {**budget, **effective}
                required_params = {
                    "tri_budget": {"max_object_tris", "max_scene_tris"},
                    "texture_budget": {"max_texture_px"},
                    "scale_range": {"min_extent_um", "max_extent_um"},
                    "light_state": {"min_energy_mw"}, "contacts": {"min_contacts", "steps"},
                    "replay_identical": {"steps"}, "interpenetration": {"max_pairs"},
                    "feet_floor": {"foot_regex", "floor_regex", "min_clearance_um"},
                    "foot_slide": {"foot_regex", "floor_regex", "plant_max_um", "max_slide_um"},
                    "bone_rotation_continuity": {"path", "max_delta_mdeg", "max_detour_mdeg"},
                    "attachment": {"prop_regex", "max_um", "max_drift_um"}}
                if kind in required_params:
                    if required_params[kind] - set(effective):
                        raise ValueError("profile_required_parameter_missing:" + kind)
                    if kind == "scale_range" and effective["min_extent_um"] > effective["max_extent_um"]:
                        raise ValueError("profile_reversed_scale_range")
                    if kind in FEET and select:
                        raise ValueError("profile_feet_checks_resolve_their_own_selection")
                    if kind == "attachment" and (select.get("type") != "MESH" or "parent_bone_regex" not in select):
                        raise ValueError("profile_attachment_requires_bone_parented_mesh_selector")
                    if kind == "bone_rotation_continuity" and (
                            select.get("type") != "ARMATURE" or "{bone}" not in effective["path"]):
                        raise ValueError("profile_bone_continuity_requires_armature_bone_path")
                elif kind == "render_settings":
                    if set(effective.get("enforce", ())) - set(profile.get("render", {})):
                        raise ValueError("profile_render_enforce_names_absent_setting")
                elif kind != "scene_camera":
                    obligation = {"kind": kind, **effective}
                    obligation.pop("steps", None)
                    if kind in TEMPORAL:
                        obligation["frames"] = [1, 2, 1]
                    validate_obligation(obligation)
    if not partial and profile["id"] == "film-animation":
        if profile["budgets"] is not None or any(c["kind"] in {"tri_budget", "texture_budget"} for c in profile["checks"]):
            raise ValueError("film_animation_has_no_asset_budgets")
    provenance = profile.get("provenance", {})
    if type(provenance) is not dict:
        raise ValueError("profile_provenance_object_required")
    for path, origin in provenance.items():
        if not path.startswith("/") or type(origin) is not str or not (
                origin == "target" or (origin.startswith(("measured:", "cited:")) and origin.split(":", 1)[1])):
            raise ValueError("profile_invalid_provenance")
    for path in numeric_paths(profile):
        if path not in provenance:
            raise ValueError("profile_number_without_provenance:" + path)
    return copy.deepcopy(profile)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("profile_duplicate_json_key:" + key)
        result[key] = value
    return result


def _merge(parent, child):
    result = copy.deepcopy(parent)
    for key, value in child.items():
        if type(value) is dict and type(result.get(key)) is dict:
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load(identifier, directory=DIRECTORY, _chain=()):
    """Resolve single inheritance. Lists replace; nested objects merge; null clears."""
    _id(identifier)
    if identifier in _chain:
        raise ValueError("profile_extends_cycle:" + identifier)
    path = Path(directory) / (identifier + ".json")
    raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    if raw.get("id") != identifier:
        raise ValueError("profile_filename_identity_mismatch")
    validate(raw, partial="extends" in raw)
    if "extends" in raw:
        parent = load(raw["extends"], directory, _chain + (identifier,))
        # Replacing a list removes its old coordinate provenance.
        for key in ("checks",):
            if key in raw:
                parent["provenance"] = {p: v for p, v in parent["provenance"].items() if not p.startswith("/" + key + "/")}
        raw = _merge(parent, raw)
    raw.pop("extends", None)
    return validate(raw)


def identity(profile):
    return {"id": profile["id"], "sha256": sha256_canonical(validate(profile))}


def resolve(identifier, directory=DIRECTORY):
    """Public inheritance resolver; returns a detached fully validated profile."""
    return load(identifier, directory)


def apply_defaults(obligation, profile):
    kind = obligation["kind"]
    return {**copy.deepcopy(profile.get("defaults", {}).get(kind, {})), **copy.deepcopy(obligation)}


def adapter_kwargs(profile, adapter_cls):
    """Explicit named controls are required; **kwargs alone is not evidence."""
    fields = inspect.signature(adapter_cls).parameters
    capture = profile["capture"]
    result = {}
    for field, key in (("width", "max_w"), ("height", "max_h")):
        if field in fields:
            result[field] = capture[key]
    if capture["engine"] != "egl":
        if not {"capture_engine", "capture_samples"} <= set(fields) or any(
                fields[name].kind == inspect.Parameter.POSITIONAL_ONLY for name in ("capture_engine", "capture_samples")):
            raise CannotSee("capture_engine_unsupported")
        result.update(capture_engine=capture["engine"], capture_samples=capture["samples"])
    return result


def _frames(state):
    for prefix in (state, state.get("scene", {})):
        if "frame_start" in prefix and "frame_end" in prefix:
            spec = [prefix["frame_start"], prefix["frame_end"], 1]
            try:
                frames_from_spec(spec)
            except ValueError as exc:
                raise CannotSee("frame_range_unsupported:" + str(exc)) from None
            return spec
    raise CannotSee("state_path_absent:/frame_start")


def _substitute(value, context):
    if type(value) is list:
        return [_substitute(item, context) for item in value]
    if type(value) is dict:
        return {_substitute(k, context): _substitute(v, context) for k, v in value.items()}
    if type(value) is str:
        for key, item in context.items():
            value = value.replace("{" + key + "}", pointer(item)[1:])
        if re.search(r"\{[^}]+\}", value):
            raise ValueError("profile_unknown_placeholder:" + value)
    return value


def resolve_floor(objects, floor_regex, exclude=()):
    """One planar floor: a MESH declared with vqa3d_role "floor", else the single
    MESH whose name matches. Its evaluated world bbox top is the floor height."""
    meshes = {name: row for name, row in objects.items() if row.get("type") == "MESH"}
    names = sorted(name for name, row in meshes.items() if row.get("role") == "floor") or sorted(
        name for name, row in meshes.items()
        if not row.get("role") and name not in exclude and re.search(floor_regex, name))
    if not names:
        raise CannotSee("floor_unresolved:declare_vqa3d_role_floor_or_name")
    if len(names) > 1:
        raise CannotSee("floor_ambiguous:" + ",".join(names))
    return names[0]


def resolve_feet(objects, foot_regex):
    """Feet as contact limbs, strongest geometry first, never silently dropping a rig.

    Parts:
      mesh: a non-skinned MESH declared vqa3d_role "foot" or matching the name regex
            (its whole evaluated bbox; its bone is its BONE parent, when it has one).
      skin: an armature-deformed MESH exposes each vertex group matching the regex
            (vertices weighted >= 0.5 to that bone); the bone is the group's namesake.
      bone: a matching armature bone that no mesh or skin part covers. Joints are not
            soles, so a bone-only foot can prove penetration but never clearance.
    Parts whose bones form a chain (foot.L -> toe.L) are one foot: its clearance is
    the lowest geometry of the chain and every chain bone is a slide segment.
    """
    parts = []
    for name, row in sorted(objects.items()):
        if row.get("type") != "MESH" or row.get("role", "") not in ("", "foot"):
            continue
        groups = sorted(g for g in row.get("vertex_groups", {}) if re.search(foot_regex, g))
        armature = row.get("skin_armature", "")
        if groups:
            for group in groups:
                bone = (armature, group) if group in objects.get(armature, {}).get("bones", {}) else None
                parts.append({"label": name + ":" + group, "source": "skin", "bone": bone,
                              "z": [pointer("objects", name, "vertex_groups", group, "world_bbox_min_um", 2)]})
        elif row.get("role") == "foot" or re.search(foot_regex, name):
            bone = (row["parent"], row["parent_bone"]) if row.get("parent_type") == "BONE" and row.get("parent_bone") else None
            parts.append({"label": name, "source": "mesh", "bone": bone,
                          "z": [pointer("objects", name, "world_bbox_min_um", 2)]})
    covered = {part["bone"] for part in parts if part["bone"]}
    for name, row in sorted(objects.items()):
        if row.get("type") != "ARMATURE":
            continue
        for bone in sorted(row.get("bones", {})):
            if re.search(foot_regex, bone) and (name, bone) not in covered:
                parts.append({"label": name + ":" + bone, "source": "bone", "bone": (name, bone),
                              "z": [pointer("objects", name, "bones", bone, key, 2) for key in ("head_world_um", "tail_world_um")]})
    bones = {part["bone"] for part in parts if part["bone"]}

    def root(bone):
        armature, name = bone
        for _ in range(64):
            parent = objects.get(armature, {}).get("bones", {}).get(name, {}).get("parent", "")
            if not parent or (armature, parent) not in bones:
                break
            name = parent
        return armature, name
    limbs = {}
    for part in parts:
        limbs.setdefault(root(part["bone"]) if part["bone"] else ("", part["label"]), []).append(part)
    feet = []
    for key, members in limbs.items():
        solid = [m for m in members if m["source"] != "bone"]
        lead = next((m for m in solid or members if m["bone"] == key), (solid or members)[0])
        feet.append({"label": lead["label"], "source": lead["source"],
                     "z": [path for m in (solid or members) for path in m["z"]],
                     "bones": sorted({list(m["bone"])[0] + "\x00" + m["bone"][1] for m in members if m["bone"]})})
    for foot in feet:
        foot["bones"] = [item.split("\x00") for item in foot["bones"]]
    return sorted(feet, key=lambda foot: foot["label"])


def is_prop(name, row, prop_regex):
    """Attachment rule: vqa3d_role decides when declared; otherwise the object
    name or one of its collections must match the prop regex. Bone-parented
    anatomy (heads, gloves, shoes) is therefore never an attachment by default."""
    role = row.get("role", "")
    if role:
        return role == "prop"
    return bool(re.search(prop_regex, name) or any(re.search(prop_regex, c) for c in row.get("collections", [])))


def _feet_steps(profile, template, params, scene_state):
    objects = scene_state.get("objects", {})
    base = {"check_id": template["id"] + ":unmatched", "severity": template["severity"], "why": template["why"],
            "action": {}, "obligation": dict(params), "frames": None, "cannot_see": None}
    feet = resolve_feet(objects, params["foot_regex"])
    if not feet:
        return [{**base, "cannot_see": "selector_empty:" + template["id"]}]
    reason, floor = None, ""
    try:
        floor = resolve_floor(objects, params["floor_regex"], {f["label"] for f in feet if f["source"] == "mesh"})
    except CannotSee as exc:
        reason = str(exc)
    spec = template["frames"]
    if spec == "scene":
        try:
            spec = _frames(scene_state)
        except CannotSee as exc:
            reason, spec = reason or str(exc), None
    steps = []
    for foot in feet:
        obligation = {**params, "foot": foot["label"], "source": foot["source"], "z_paths": foot["z"], "floor": floor,
                      "floor_path": pointer("objects", floor, "world_bbox_max_um", 2) if floor else ""}
        if foot["bones"]:
            obligation["segments"] = [[pointer("objects", armature, "bones", bone, end) for end in ("head_world_um", "tail_world_um")]
                                      for armature, bone in foot["bones"]]
        if spec is not None:
            obligation["frames"] = spec
        steps.append({**base, "check_id": template["id"] + ":" + foot["label"], "obligation": obligation,
                      "frames": spec, "cannot_see": reason})
    return steps


def expand(profile, scene_state):
    """Return ordered profile scenario steps; refusals are explicit scenario data."""
    validate(profile)
    result = []
    objects = scene_state.get("objects", {})
    for template in profile["checks"]:
        select = template["select"]
        if template["kind"] in FEET:
            result.extend(_feet_steps(profile, template, apply_defaults(
                {"kind": template["kind"], **template["params"]}, profile), scene_state))
            continue
        matches = [("scene", {})] if not select else [
            (name, row) for name, row in sorted(objects.items())
            if row.get("type") == select["type"] and
            re.search(select.get("name_regex", ".*"), name) and
            ("parent_bone_regex" not in select or (row.get("parent_type") == "BONE" and
             re.search(select["parent_bone_regex"], row.get("parent_bone", ""))))]
        reason = None if matches else "selector_empty:" + template["id"]
        if template["kind"] == "attachment" and matches:
            regex = apply_defaults({"kind": "attachment", **template["params"]}, profile)["prop_regex"]
            candidates = len(matches)
            matches = [(name, row) for name, row in matches if is_prop(name, row, regex)]
            if not matches:
                # Structure alone cannot tell a prop from rigid anatomy: stay unresolved.
                reason = "selector_empty:%s:undeclared_bone_parented_meshes=%d" % (template["id"], candidates)
        if not matches:
            matches = [("unmatched", {})]
        for name, row in matches:
            params = apply_defaults({"kind": template["kind"], **template["params"]}, profile)
            if template["kind"] in {"tri_budget", "texture_budget"}:
                fields = PARAMS[template["kind"]]
                params = {**{k: v for k, v in (profile["budgets"] or {}).items() if k in fields}, **params}
            bones = sorted(row.get("bones", {})) if any("{bone}" in str(v) for v in params.values()) else [None]
            if not bones:
                bones = [None]
            for bone in bones:
                context = {"object": name, "parent": row.get("parent", ""), "parent_bone": row.get("parent_bone", "")}
                local_reason = reason
                if bone is not None:
                    context["bone"] = bone
                elif any("{bone}" in str(v) for v in params.values()):
                    local_reason = local_reason or "state_path_absent:" + pointer("objects", name, "bones")
                    context["bone"] = "AUSENTE"
                obligation = {k: _substitute(v, context) for k, v in params.items()}
                spec = template["frames"]
                if obligation["kind"] == "attachment":
                    obligation.update(a=pointer("objects", name, "world_location_um"),
                                      head=pointer("objects", context["parent"], "bones", context["parent_bone"], "head_world_um"),
                                      tail=pointer("objects", context["parent"], "bones", context["parent_bone"], "tail_world_um"))
                if spec == "scene" and obligation["kind"] in TEMPORAL | PIXELS | SHOT:
                    try:
                        spec = _frames(scene_state)
                    except CannotSee as exc:
                        local_reason = local_reason or str(exc)
                        spec = None
                if obligation["kind"] in TEMPORAL | SHOT and spec is not None:
                    obligation["frames"] = frames_from_spec(spec) if obligation["kind"] == "within_distance" else spec
                check_id = template["id"] + ":" + name + ((":" + bone) if bone else "")
                result.append({"check_id": check_id, "severity": template["severity"], "why": template["why"],
                               "action": {}, "obligation": obligation, "frames": spec,
                               "cannot_see": local_reason})
    return result


def summary(receipts, profile):
    gates = [r["final_verdict"] for r in receipts if r.get("severity") == "gate"]
    verdict = max_precedence(gates) if gates else "CANNOT_SEE"
    if verdict == "PASS" and any(r["final_verdict"] != "PASS" for r in receipts if r.get("severity") == "advisory"):
        verdict = "ADVISORY"
    return {"schema": "vqa3d.profile-summary/v1", "profile": identity(profile),
            "final_verdict": verdict, "verdict_rc": rc_of(verdict), "checks": len(receipts)}


def _get(state, path):
    try:
        return lookup(state, path)
    except (KeyError, IndexError, TypeError):
        raise CannotSee("state_path_absent:" + path) from None


def _observation(adapter, state):
    camera = state.get("source_camera", "")
    return Observation.create(adapter.id, "profile-state", state,
                              camera_source="scene" if camera else "none", camera_name=camera)


def _receipt(adapter, profile, step, verdict, reason, observation=None):
    receipt = make_receipt(adapter, step["obligation"], observation, observation,
                           [{"oracle": step["obligation"]["kind"], "verdict": verdict, "reason": reason}])
    receipt.update(profile=identity(profile), check_id=step["check_id"], severity=step["severity"], why=step["why"])
    receipt.pop("receipt_sha256", None)
    receipt["receipt_sha256"] = sha256_canonical(receipt)
    return receipt


def blender_metadata(adapter):
    """Read source metadata in a bounded owned Blender process, never save it.

    The base adapter omits scene frame bounds and overwrites scratch render
    settings. This separate read-only query retains the original source values.
    It does not fill missing mesh/light/image/bone interfaces.
    """
    import hashlib
    import subprocess
    import tempfile
    from blender_visual_qa.drive.launch import declared_env, _owned_process
    from .adapters.blender import BLENDER
    expression = (
        "import bpy,json; s=bpy.context.scene; "
        "print('VQA3D_PROFILE_METADATA='+json.dumps({'frame_start':s.frame_start,'frame_end':s.frame_end,"
        "'source_camera':s.camera.name if s.camera else '',"
        "'render':{'engine':s.render.engine,'samples':s.cycles.samples,"
        "'view_transform':s.view_settings.view_transform,'look':s.view_settings.look,"
        "'motion_blur':int(s.render.use_motion_blur),'resolution_pct':s.render.resolution_percentage,"
        "'clamp_indirect_milli':int(round(s.cycles.sample_clamp_indirect*1000)),"
        "'clamp_direct_milli':int(round(s.cycles.sample_clamp_direct*1000)),"
        "'adaptive_sampling':int(s.cycles.use_adaptive_sampling),"
        "'adaptive_threshold_micro':int(round(s.cycles.adaptive_threshold*1000000)),"
        "'denoising':int(s.cycles.use_denoising),'device':s.cycles.device,"
        "'resolution_px':[s.render.resolution_x,s.render.resolution_y],'fps':s.render.fps}},sort_keys=True))")
    source = Path(adapter.source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="vqa3d-profile-meta-") as scratch:
        argv = [str(BLENDER), "-b", "--factory-startup", "-noaudio", "--disable-autoexec", str(source),
                "--python-exit-code", "9", "--python-expr", expression]
        with _owned_process(argv, env=declared_env(Path(scratch), 1), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT) as process:
            output, _ = process.communicate(timeout=90)
        text = output.decode("utf-8", "replace")
        adapter.process_logs.append({"mode": "profile_metadata", "rc": process.returncode, "output": text[-12000:]})
        rows = [line.split("=", 1)[1] for line in text.splitlines() if line.startswith("VQA3D_PROFILE_METADATA=")]
        if process.returncode != 0 or len(rows) != 1:
            raise CannotSee("source_metadata_unavailable")
        if hashlib.sha256(source.read_bytes()).hexdigest() != before:
            raise CannotSee("source_changed_during_metadata")
        result = json.loads(rows[0])
        validate_json(result, allow_bool=False)
        result["source_sha256"] = before
        return result


def probe_adapter(adapter, profile):
    fields = inspect.signature(adapter.probe).parameters
    return adapter.probe(render=True) if profile["requires"] and "render" in fields else adapter.probe()


def _replay(profile, factory, steps):
    from .fakes import RecordingAdapter, compare_replay_runs
    traces = []
    for _ in range(2):
        adapter = factory()
        adapter.profile_identity = identity(profile)
        recorder = RecordingAdapter(adapter)
        recorder.profile_identity = identity(profile)
        try:
            scenario = ([{"action": {"op": "step", "n": steps},
                          "obligation": apply_defaults({"kind": "frame_change"}, profile)}]
                        if adapter.id == "mujoco" else
                        [{"action": {"op": "set_frame", "frame": _get(adapter.scene_state(), "/frame_current")},
                          "obligation": {"kind": "no_change"}}])
            receipts = [verify_step(recorder, step["action"], step["obligation"]) for step in scenario]
            if any(r["final_verdict"] not in {"PASS", "ADVISORY"} or
                   not all(r[tag]["capture_available"] for tag in ("before", "after")) for r in receipts):
                raise CannotSee("replay_observations_unqualified")
            traces.append(recorder.trace(scenario, receipts, {"environment": "MuJoCo simulation" if adapter.id == "mujoco" else "Blender",
                                                           "profile": identity(profile), "scope": "same host and runtime only"}))
        finally:
            adapter.close()
    comparison = compare_replay_runs(traces[0]["receipts"], traces[1]["receipts"])
    return comparison["final_verdict"], comparison["reason"], traces


def _rows(adapter, frames, paths):
    """Sample integer state over frames; any missing leaf is unobservable, never zero."""
    paths = list(dict.fromkeys(paths))
    samples = sample_adapter(adapter, frames, paths)
    rows = []
    for frame in frames:
        row = samples.get(str(frame), samples.get(frame))
        for path in paths:
            if type(row) is not dict or path not in row:
                raise CannotSee("state_path_absent:" + path)
        rows.append(row)
    return rows


def _peak(values, frames):
    """Largest value and the earliest frame that reaches it."""
    high = max(values)
    return high, frames[values.index(high)]


def _xyz(value, path):
    if type(value) is not list or len(value) != 3 or any(type(v) is not int for v in value):
        raise CannotSee("state_path_invalid:" + path)
    return value


def quat_angle_mdeg(a, b):
    """Angle of the rotation taking unit quaternion a to b ([w,x,y,z] ppm), in mdeg.

    Uses atan2(|vector part|, |scalar part|) of conj(a)*b: sign-invariant (q = -q)
    and well conditioned at small angles, unlike acos of the dot product."""
    w = sum(x*y for x, y in zip(a, b))
    vector = (a[0]*b[1] - a[1]*b[0] - a[2]*b[3] + a[3]*b[2],
              a[0]*b[2] + a[1]*b[3] - a[2]*b[0] - a[3]*b[1],
              a[0]*b[3] - a[1]*b[2] + a[2]*b[1] - a[3]*b[0])
    return round(math.degrees(2 * math.atan2(math.isqrt(sum(v*v for v in vector)), abs(w))) * 1000)


def _quat(value, path):
    if type(value) is not list or len(value) != 4 or any(type(v) is not int for v in value):
        raise CannotSee("state_path_invalid:" + path)
    if abs(sum(v*v for v in value) - 10**12) > 10**8:  # |q| within 0.005 % of one
        raise CannotSee("state_path_invalid:non_unit_quaternion:" + path)
    return value


def _clearances(adapter, obligation):
    """Per-frame sole-to-floor clearance in micrometres for one resolved foot part."""
    frames = frames_from_spec(obligation["frames"])
    extra = [path for segment in obligation.get("segments", []) for path in segment]
    rows = _rows(adapter, frames, obligation["z_paths"] + [obligation["floor_path"]] + extra)
    clearance = [min(integer(row[path]) for path in obligation["z_paths"]) - integer(row[obligation["floor_path"]])
                 for row in rows]
    return frames, rows, clearance


def _feet_floor(adapter, obligation):
    frames, _, clearance = _clearances(adapter, obligation)
    low, frame = min(zip(clearance, frames))
    detail = "%s:frame=%d:clearance_um=%d:floor=%s:source=%s" % (obligation["foot"], frame, low, obligation["floor"], obligation["source"])
    if low < obligation["min_clearance_um"]:
        return "FAIL", "foot_below_floor:" + detail
    if obligation["source"] == "bone":
        # Joint positions bound penetration from one side only.
        raise CannotSee("sole_geometry_unresolved:foot_bone_joints_clear:" + detail)
    return "PASS", "feet_clear:" + detail


def _foot_slide(adapter, obligation):
    """Net horizontal drift of the best fixed point on the ankle-to-toe line during
    each run of consecutive planted frames. A foot pivoting or rolling about a
    point of that line does not slide; a dragged foot does."""
    if obligation["source"] == "bone":
        raise CannotSee("sole_geometry_unresolved:" + obligation["foot"])
    if not obligation.get("segments"):
        raise CannotSee("foot_bone_unresolved:" + obligation["foot"])
    if obligation["frames"][2] != 1:
        raise CannotSee("foot_slide_requires_step_one")
    frames, rows, clearance = _clearances(adapter, obligation)
    points = [[(_xyz(row[head], head), _xyz(row[tail], tail)) for head, tail in obligation["segments"]] for row in rows]
    runs, start = [], None
    for index, value in enumerate(clearance + [obligation["plant_max_um"] + 1]):
        planted = value <= obligation["plant_max_um"]
        if planted and start is None:
            start = index
        elif not planted and start is not None:
            if index - start >= 2:
                runs.append((start, index - 1))
            start = None
    if not runs:
        low, frame = min(zip(clearance, frames))
        raise CannotSee("foot_never_planted:%s:min_clearance_um=%d:frame=%d:plant_max_um=%d" % (
            obligation["foot"], low, frame, obligation["plant_max_um"]))
    worst = None
    for first, last in runs:
        best = None
        for segment in range(len(obligation["segments"])):
            for k in range(SLIDE_CANDIDATES + 1):
                def at(index):
                    head, tail = points[index][segment]
                    return [(SLIDE_CANDIDATES - k) * head[axis] + k * tail[axis] for axis in (0, 1)]
                origin = at(first)
                reach = max(sum((v - o) ** 2 for v, o in zip(at(index), origin)) for index in range(first, last + 1))
                best = reach if best is None else min(best, reach)
        drift = math.isqrt(best) // SLIDE_CANDIDATES
        if worst is None or drift > worst[0]:
            worst = (drift, frames[first], frames[last])
    detail = "%s:frames=%d-%d:drift_um=%d:max_um=%d:planted_runs=%d:plant_max_um=%d" % (
        obligation["foot"], worst[1], worst[2], worst[0], obligation["max_slide_um"], len(runs), obligation["plant_max_um"])
    return ("FAIL", "foot_slide:" + detail) if worst[0] > obligation["max_slide_um"] else ("PASS", "planted_feet_hold:" + detail)


def _bone_rotation(adapter, obligation):
    """Adjacent-frame joint rotation: a step ceiling plus a one-frame detour ceiling.

    detour(f) = angle(f-1,f) + angle(f,f+1) - angle(f-1,f+1): zero for monotone
    motion about one axis, twice the excursion for a pose that pops for one frame."""
    frames = frames_from_spec(obligation["frames"])
    if len(frames) < 2:
        raise CannotSee("continuity_needs_adjacent_frames")
    if obligation["frames"][2] != 1:
        raise CannotSee("continuity_requires_step_one")
    path = obligation["path"]
    quats = [_quat(row[path], path) for row in _rows(adapter, frames, [path])]
    steps = [quat_angle_mdeg(a, b) for a, b in zip(quats, quats[1:])]
    delta, delta_frame = _peak(steps, frames[1:])
    detours = [steps[i] + steps[i+1] - quat_angle_mdeg(quats[i], quats[i+2]) for i in range(len(steps) - 1)]
    detour, detour_frame = _peak(detours, frames[1:-1]) if detours else (0, frames[0])
    detail = "max_delta_mdeg=%d:frame=%d:max_detour_mdeg=%d:detour_frame=%d" % (delta, delta_frame, detour, detour_frame)
    if detour > obligation["max_detour_mdeg"]:
        return "FAIL", "bone_rotation_pop:%s:frame=%d:detour_mdeg=%d:%s" % (path, detour_frame, detour, detail)
    if delta > obligation["max_delta_mdeg"]:
        return "FAIL", "bone_rotation_step:%s:frame=%d:delta_mdeg=%d:%s" % (path, delta_frame, delta, detail)
    return "PASS", "bone_rotation_continuous:" + detail


def _attachment(adapter, obligation):
    """A declared prop stays near its bone tail and does not slip along the bone."""
    frames = frames_from_spec(obligation["frames"])
    keys = ("a", "head", "tail")
    spans = {"head": [], "tail": []}
    for row in _rows(adapter, frames, [obligation[k] for k in keys]):
        origin = _xyz(row[obligation["a"]], obligation["a"])
        for end in spans:
            spans[end].append(math.isqrt(sum((x - y) ** 2 for x, y in zip(origin, _xyz(row[obligation[end]], obligation[end])))))
    reach, frame = _peak(spans["tail"], frames)
    drift = max(max(values) - min(values) for values in spans.values())
    detail = "max_tail_distance_um=%d:frame=%d:max_um=%d:drift_um=%d:max_drift_um=%d:frames=%d" % (
        reach, frame, obligation["max_um"], drift, obligation["max_drift_um"], len(frames))
    if reach > obligation["max_um"]:
        return "FAIL", "attachment_distance_exceeded:" + detail
    if drift > obligation["max_drift_um"]:
        return "FAIL", "attachment_slip:" + detail
    return "PASS", "attachment_held:" + detail


def pixel_frames(spec, max_frames=None):
    """Frames a pixel check renders: all of them, or max_frames evenly spaced ones
    that always include both ends. Sparse coverage is declared in the reason."""
    frames = frames_from_spec(spec)
    if max_frames is None or len(frames) <= max_frames:
        return frames
    if max_frames == 1:
        return frames[:1]
    last = len(frames) - 1
    return [frames[(i * last + (max_frames - 1) // 2) // (max_frames - 1)] for i in range(max_frames)]


def _state_oracle(adapter, profile, step, state, probe, factory):
    obligation = step["obligation"]
    kind = obligation["kind"]
    if kind in SHOT:
        oracle = {"feet_floor": _feet_floor, "foot_slide": _foot_slide,
                  "bone_rotation_continuity": _bone_rotation, "attachment": _attachment}[kind]
        try:
            return (*oracle(adapter, obligation), None)
        except (KeyError, IndexError):
            raise CannotSee("state_path_absent:" + str(obligation.get("path", obligation.get("foot", obligation.get("a", ""))))) from None
    if kind == "capability":
        return (*capability_check(probe, obligation), None)
    if kind == "state_change":
        raise CannotSee("profile_action_absent")
    if kind in {"state_equals", "camera_is"}:
        if kind == "state_equals":
            for path in obligation["expected"]:
                _get(state, path)
        else:
            _get(state, "/source_camera")
        observation = _observation(adapter, state)
        return (*evaluate(observation, observation, obligation), None)
    if kind in TEMPORAL:
        for key in (("a", "b") if kind == "within_distance" else ("path",)):
            _get(state, obligation[key])
        if kind == "range_over_frames":
            frames = frames_from_spec(obligation["frames"])
            path = obligation["path"]
            try:
                samples = sample_adapter(adapter, frames, [path])
                from .temporal import values
                points = [(integer(value), frame, index) for frame in frames
                          for index, value in enumerate(values(samples.get(str(frame), samples.get(frame))[path]))]
            except (KeyError, IndexError):
                raise CannotSee("state_path_absent:" + path) from None
            low, high = min(points), max(points)
            bad_low = "min" in obligation and low[0] < obligation["min"]
            bad_high = "max" in obligation and high[0] > obligation["max"]
            value, frame, index = low if bad_low or not bad_high else high
            reason = (f"range_exceeded:{path}:frame={frame}:element={index}:value={value}" if bad_low or bad_high else
                      f"frame_samples_within_bounds:{path}:minimum={low[0]}:frame={low[1]}:maximum={high[0]}:max_frame={high[1]}")
            return ("FAIL" if bad_low or bad_high else "PASS"), reason, None
        if kind == "max_delta_over_frames" and len(frames_from_spec(obligation["frames"])) < 2:
            raise CannotSee("continuity_needs_adjacent_frames")
        if kind == "max_delta_over_frames" and obligation["frames"][2] != 1:
            raise CannotSee("continuity_requires_step_one")
        if kind == "max_delta_over_frames" and obligation["path"].endswith("/rotation_mdeg"):
            frames = frames_from_spec(obligation["frames"])
            path = obligation["path"]
            try:
                samples = sample_adapter(adapter, frames, [path])
                previous = None
                for frame in frames:
                    row = samples.get(str(frame), samples.get(frame))
                    from .temporal import vector
                    current = vector(row[path])
                    if previous is not None:
                        delta = max(abs((b-a+180000) % 360000-180000) for a, b in zip(previous, current))
                        if delta > obligation["max_delta"]:
                            return "FAIL", f"bone_rotation_pop:{path}:frame={frame}:delta_mdeg={delta}", None
                    previous = current
            except (KeyError, IndexError):
                raise CannotSee("state_path_absent:" + path) from None
            return "PASS", "adjacent_bone_euler_deltas_within_bounds", None
        try:
            verdict, reason = evaluate_temporal(adapter, state, obligation)
        except (KeyError, IndexError):
            raise CannotSee("state_path_absent:" + obligation.get("path", obligation.get("b", ""))) from None
        return verdict, reason, None
    if kind == "scene_camera":
        camera = _get(state, "/source_camera")
        return ("PASS", "scene_camera:" + camera, None) if camera else ("FAIL", "scene_camera_absent:no_fallback_allowed", None)
    if kind == "render_settings":
        # Unlisted settings are recorded shot choices, not quality gates.
        enforced = obligation.get("enforce", sorted(profile["render"]))
        for key in sorted(enforced):
            expected, actual = profile["render"][key], _get(state, pointer("render", key))
            if (integer(actual) < expected if key == "samples" else actual != expected):
                return "FAIL", f"render_setting_mismatch:{key}:expected={expected}:actual={actual}", None
        source = _get(state, "/render")
        seen = [f"{key}={source[key]}" + ("" if key in enforced else "(recorded)")
                for key in sorted(source) if type(source[key]) in (int, str)]
        return "PASS", "source_render_settings_match:" + ":".join(seen), None
    if kind == "tri_budget":
        rows = [(name, _get(state, pointer("objects", name, "tris"))) for name, row in sorted(state.get("objects", {}).items()) if row.get("type") == "MESH"]
        if not rows:
            raise CannotSee("selector_empty:" + step["check_id"])
        for name, value in rows:
            integer(value)
            if value < 0:
                raise CannotSee("state_path_invalid:" + pointer("objects", name, "tris"))
            if value > obligation["max_object_tris"]:
                return "FAIL", f"object_tri_budget:{name}:actual={value}:max={obligation['max_object_tris']}", None
        total = sum(value for _, value in rows)
        return ("FAIL" if total > obligation["max_scene_tris"] else "PASS"), f"scene_tris={total}:max={obligation['max_scene_tris']}", None
    if kind == "texture_budget":
        images = _get(state, "/images")
        for name in sorted(images):
            size = _get(state, pointer("images", name, "size_px"))
            if type(size) is not list or len(size) != 2 or any(type(v) is not int or v <= 0 for v in size):
                raise CannotSee("state_path_invalid:" + pointer("images", name, "size_px"))
            if max(size) > obligation["max_texture_px"]:
                return "FAIL", f"texture_budget:{name}:size_px={size}:max={obligation['max_texture_px']}", None
        return "PASS", "texture_sizes_within_budget:images=" + str(len(images)), None
    if kind == "scale_range":
        name = step["check_id"].split(":", 1)[1]
        low = _get(state, pointer("objects", name, "world_bbox_min_um"))
        high = _get(state, pointer("objects", name, "world_bbox_max_um"))
        from .temporal import vector
        extents = [b-a for a, b in zip(vector(low), vector(high))]
        good = all(obligation["min_extent_um"] <= v <= obligation["max_extent_um"] for v in extents)
        return ("PASS" if good else "FAIL"), "world_bbox_extents_um=" + canonical_json(extents), None
    if kind == "light_state":
        name = step["check_id"].split(":", 1)[1]
        energy = _get(state, pointer("objects", name, "energy_mw"))
        color = _get(state, pointer("objects", name, "color_ppm"))
        good = integer(energy) >= obligation["min_energy_mw"] and type(color) is list and len(color) == 3 and all(type(v) is int and 0 <= v <= 1000000 for v in color)
        return ("PASS" if good else "FAIL"), f"light_energy_mw={energy}:color_ppm={color}", None
    if kind == "contacts":
        if adapter.id != "mujoco":
            raise CannotSee("simulation_adapter_required")
        action = adapter.act({"op": "step", "n": obligation["steps"]})
        if action.get("ok") is not True:
            return "FAIL", "simulation_step_failed", None
        current = adapter.scene_state()
        count = integer(_get(current, "/ncon"))
        _get(current, "/contact_geom_pairs")
        return ("PASS" if count >= obligation["min_contacts"] else "FAIL"), f"simulation_contacts={count}", current
    if kind == "interpenetration":
        pairs = _get(state, "/interpenetration_pairs")
        if type(pairs) is not list:
            raise CannotSee("state_path_invalid:/interpenetration_pairs")
        return ("PASS" if len(pairs) <= obligation["max_pairs"] else "FAIL"), "interpenetration_pairs=" + str(len(pairs)), None
    if kind == "replay_identical":
        if factory is None:
            raise CannotSee("independent_replay_factory_absent")
        if profile["id"] == "simulation" and adapter.id != "mujoco":
            raise CannotSee("simulation_adapter_required")
        return _replay(profile, factory, obligation["steps"])
    raise ValueError("profile_unknown_kind:" + kind)


def run(profile, adapter, *, frames=None, capture_reason=None, factory=None, metadata=None, emit=None):
    """Probe/requires first; independent unsupported checks never suppress others."""
    validate(profile)
    adapter.profile_identity = identity(profile)
    from .fakes import RecordingAdapter
    if not isinstance(adapter, RecordingAdapter):
        adapter = RecordingAdapter(adapter)
        adapter.profile_identity = identity(profile)
    records, replay_traces, observations = [], [], []
    def append(step, verdict, reason, observation=None):
        receipt = _receipt(adapter, profile, step, verdict, reason, observation)
        records.append(receipt)
        if emit:
            emit(receipt)
    requirement = {"check_id": "requires", "severity": "gate", "why": "Qualify requested runtime capabilities before dependent checks.",
                   "obligation": {"kind": "capability", **profile["requires"]}}
    try:
        probe = probe_adapter(adapter, profile)
        verdict, reason = capability_check(probe, requirement["obligation"]) if profile["requires"] else (
            ("PASS", "adapter_available") if probe.get("available") is True else ("CANNOT_SEE", "adapter_unavailable:" + str(probe.get("reason"))))
    except Exception as exc:
        probe, verdict, reason = {}, "CANNOT_SEE", "probe_unavailable:" + type(exc).__name__ + ":" + str(exc)
    append(requirement, verdict, reason)
    pixel_reason = capture_reason or (reason if verdict != "PASS" else None)
    if callable(metadata):
        try:
            metadata = metadata()
        except Exception as exc:
            metadata = {"source_metadata_error": type(exc).__name__ + ":" + str(exc)}
    try:
        state = adapter.scene_state()
        validate_json(state, allow_bool=False)
        state = {**state, **(metadata or {})}
        if frames is not None:
            frames_from_spec(frames)
            state.update(frame_start=frames[0], frame_end=frames[1])
        scenario = expand(profile, state)
        if frames is not None:
            for step in scenario:
                if step["obligation"]["kind"] in TEMPORAL | PIXELS | SHOT:
                    step["frames"] = frames
                    if step["obligation"]["kind"] in TEMPORAL | SHOT:
                        step["obligation"]["frames"] = frames_from_spec(frames) if step["obligation"]["kind"] == "within_distance" else frames
    except Exception as exc:
        state = {}
        scenario = [{"check_id": t["id"], "severity": t["severity"], "why": t["why"],
                     "obligation": {"kind": t["kind"]}, "cannot_see": "scene_state_unavailable:" + type(exc).__name__ + ":" + str(exc)} for t in profile["checks"]]
    # Capture each requested frame once for all image metrics. State checks do not render.
    image_cache = {}
    shot_camera = any(check["kind"] == "scene_camera" for check in profile["checks"])
    for step in scenario:
        try:
            if step.get("cannot_see"):
                raise CannotSee(step["cannot_see"])
            kind = step["obligation"]["kind"]
            observation = _observation(adapter, state)
            if kind in PIXELS:
                if pixel_reason:
                    raise CannotSee(pixel_reason)
                if kind == "frame_change" and adapter.id == "mujoco":
                    receipt = verify_step(adapter, {"op": "step", "n": step["obligation"].get("steps", 120)},
                                          {k: v for k, v in step["obligation"].items() if k != "steps"})
                    verdict, reason = receipt["final_verdict"], receipt["error"] or receipt["verdicts"][0]["reason"]
                    observation = None
                    replay_traces.append({"simulation_frame_change_receipt": receipt})
                else:
                    verdict, reasons = "PASS", []
                    previous = None
                    original = _get(state, "/frame_current")
                    wanted = pixel_frames(step["frames"], profile["capture"].get("max_frames"))
                    total = len(frames_from_spec(step["frames"]))
                    try:
                        for frame in wanted:
                            if frame not in image_cache:
                                action = adapter.act({"op": "set_frame", "frame": frame})
                                if action.get("ok") is not True:
                                    raise CannotSee("capture_frame_action_failed")
                                obs = adapter.capture("profile-frame:" + str(frame))
                                obs.validate(adapter.id, "profile-frame:" + str(frame))
                                if shot_camera and obs.camera_source != "scene":
                                    # A fallback view cannot qualify pixels of an authored shot.
                                    raise CannotSee("capture_camera_not_scene:" + str(obs.camera_source))
                                image_cache[frame] = obs
                                from .fakes import observation_record
                                observations.append(observation_record(obs, "profile-frame:" + str(frame)))
                            observation = image_cache[frame]
                            if kind in {"no_change", "frame_change"} and previous is None:
                                previous = observation
                                continue
                            current, detail = evaluate(previous or observation, observation, step["obligation"])
                            reasons.append(f"frame={frame}:" + detail)
                            verdict = max_precedence([verdict, current])
                            previous = observation
                        if not reasons:
                            raise CannotSee("pixel_comparison_needs_two_frames")
                        reason = ";".join(reasons)
                        if len(wanted) < total:
                            reason = "frames_sampled=%d_of_%d;" % (len(wanted), total) + reason
                    finally:
                        restored = adapter.act({"op": "set_frame", "frame": original})
                        if restored.get("ok") is not True:
                            raise CannotSee("capture_frame_restore_failed")
            else:
                if kind == "replay_identical" and pixel_reason:
                    raise CannotSee(pixel_reason)
                verdict, reason, extra = _state_oracle(adapter, profile, step, state, probe, factory)
                if kind == "replay_identical":
                    replay_traces.extend(extra)
                elif kind == "contacts":
                    observation = _observation(adapter, extra)
            append(step, verdict, reason, observation)
        except CannotSee as exc:
            append(step, "CANNOT_SEE", str(exc))
        except Exception as exc:
            append(step, "ERROR", type(exc).__name__ + ":" + str(exc))
    result = summary(records, profile)
    if emit:
        emit(result)
    trace = {"schema": "vqa3d.profile-trace/v1", "profile": identity(profile), "probe": probe,
             "scene_state": state, "scenario": scenario, "receipts": records, "summary": result,
             "observations": observations, "events": adapter.events, "replay_runs": replay_traces,
             "provenance": {"recording": "live", "environment": "MuJoCo simulation" if adapter.id == "mujoco" else adapter.id,
                            "scope": "same host and runtime only", "source_sha256": (metadata or {}).get("source_sha256")}}
    trace["trace_sha256"] = sha256_canonical(trace)
    return records, result, trace
