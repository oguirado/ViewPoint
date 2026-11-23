"""Local F2 contracts; no dependency on an unlanded F0 kernel."""
import hashlib
import json

SCHEMA = "af.mesh-regression/v1"
STATUS = {0: "OK", 2: "ERROR", 3: "INCOMPLETE", 4: "STALE", 5: "FAIL", 6: "BUDGET_EXCEEDED"}
DISTANCE_MAX_PPM = 20000  # DESIGN_CHOICE, uncalibrated; INTERFACES_F0 section 2.8.
IOU_MIN_PPM = 950000  # DESIGN_CHOICE, uncalibrated.
MAX_BYTES = 16 * 2**20  # TARGET: bounded GLB and receipt ingestion.
METRICS = ("hausdorff_forward_um", "hausdorff_backward_um", "raw_diag_um",
           "hausdorff_relative_ppm", "silhouette_iou_ppm", "component_inventories")


class MeshError(Exception):
    def __init__(self, rc, code):
        super().__init__(code)
        self.rc, self.code = rc, code


def integer_tree(obj):
    if obj is None or type(obj) in (str, int, bool):
        return
    if isinstance(obj, list):
        for value in obj:
            integer_tree(value)
        return
    if isinstance(obj, dict) and all(type(key) is str for key in obj):
        for value in obj.values():
            integer_tree(value)
        return
    raise MeshError(2, "non_integer_number_or_type")


def canonical(obj):
    integer_tree(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False) + "\n"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_digest(obj):
    return digest(canonical(obj).encode("utf-8"))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MeshError(2, "duplicate_key")
        result[key] = value
    return result


def loads(data):
    def bad(_):
        raise MeshError(2, "non_integer_number")
    try:
        return json.loads(data, object_pairs_hook=_pairs, parse_float=bad, parse_constant=bad)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise MeshError(2, "malformed_json") from exc


def valid_hash(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def validate_policy(policy):
    integer_tree(policy)
    required = {"sample_budget", "raster_resolution", "view_basis", "unit", "transform",
                "weld_tolerance_um", "allow_open", "require_uv", "component_allowlist", "timeout_s"}
    optional = {"target_faces", "repair", "toolchain_sha256"}
    if not isinstance(policy, dict) or not required <= policy.keys() or policy.keys() - required - optional:
        raise MeshError(2, "policy_keys")
    bounds = {"sample_budget": (1, 100000), "raster_resolution": (8, 512),
              "weld_tolerance_um": (0, 1000000), "timeout_s": (1, 300)}
    for key, (lo, hi) in bounds.items():
        if type(policy[key]) is not int or not lo <= policy[key] <= hi:
            raise MeshError(2, "policy_" + key)
    if (policy["view_basis"] != "gltf-y-up-ortho4" or policy["unit"] != "metre"
            or policy["transform"] != "world"):
        raise MeshError(2, "unsupported_convention")
    for key in ("allow_open", "require_uv"):
        if type(policy[key]) is not bool:
            raise MeshError(2, "policy_" + key)
    allow = policy["component_allowlist"]
    if not isinstance(allow, dict) or not allow or any(
            not name or type(count) is not int or count < 1 for name, count in allow.items()):
        raise MeshError(2, "component_allowlist")
    if "target_faces" in policy and (type(policy["target_faces"]) is not int or policy["target_faces"] < 1):
        raise MeshError(2, "target_faces")
    if "toolchain_sha256" in policy and not valid_hash(policy["toolchain_sha256"]):
        raise MeshError(2, "toolchain_pin")
    if "repair" in policy:
        repair = policy["repair"]
        if not isinstance(repair, dict) or set(repair) != {"max_passes", "max_hole_edges", "cull_max_permille"}:
            raise MeshError(2, "repair_policy")
        for key, lo, hi in (("max_passes", 1, 4), ("max_hole_edges", 0, 64), ("cull_max_permille", 0, 1000)):
            if type(repair[key]) is not int or not lo <= repair[key] <= hi:
                raise MeshError(2, "repair_" + key)
        if policy["allow_open"] and repair["max_hole_edges"]:
            raise MeshError(2, "intentional_openings_protected")


def receipt(raw_hash=None, candidate_hash=None, policy_hash=None, tool_hash=None):
    return {"schema": SCHEMA, "rc": 3, "status": STATUS[3], "input_sha256": raw_hash,
            "candidate_sha256": candidate_hash, "prev_sha256": None, "policy_sha256": policy_hash,
            "toolchain_sha256": tool_hash, "metrics": {key: None for key in METRICS},
            "failed": [], "unavailable": {key: "not_evaluated" for key in METRICS}}


def finish(rec, rc, code=None):
    # Preserve proven failures when a subsequent check cannot see. Keep the
    # unavailable reason on the receipt independently of the aggregate status.
    if rc in (3, 4, 6) and rec["failed"]:
        if code:
            rec["unavailable"]["evaluation"] = code
        rc, code = 5, None
    rec["rc"], rec["status"] = rc, STATUS[rc]
    rec.pop("artefact_sha256", None)
    if code:
        if rc == 5:
            rec["failed"] = sorted(set(rec["failed"] + [code]))
        else:
            rec["unavailable"]["evaluation"] = code
    if rc == 0:
        rec["artefact_sha256"] = rec["candidate_sha256"] or rec["input_sha256"]
    return rc, rec


def validate_receipt(rec):
    integer_tree(rec)
    required = set(receipt())
    if not isinstance(rec, dict) or not required <= rec.keys() or rec.keys() - required - {"artefact_sha256"}:
        raise MeshError(2, "receipt_keys")
    rc = rec["rc"]
    if rec["schema"] != SCHEMA or type(rc) is not int or rc not in STATUS or rec["status"] != STATUS[rc]:
        raise MeshError(2, "receipt_schema")
    for key in ("input_sha256", "candidate_sha256", "prev_sha256", "policy_sha256", "toolchain_sha256"):
        if rec[key] is not None and not valid_hash(rec[key]):
            raise MeshError(2, "receipt_hash")
    if (not isinstance(rec["metrics"], dict) or not set(METRICS) <= rec["metrics"].keys()
            or not isinstance(rec["failed"], list) or any(type(x) is not str for x in rec["failed"])
            or not isinstance(rec["unavailable"], dict)
            or any(type(x) is not str or not x for x in rec["unavailable"].values())):
        raise MeshError(2, "receipt_evidence")
    for key in METRICS:
        value = rec["metrics"][key]
        if value is None:
            if key not in rec["unavailable"]:
                raise MeshError(2, "null_metric_without_reason")
        elif key == "component_inventories":
            if not isinstance(value, list):
                raise MeshError(2, "receipt_components")
        elif type(value) is not int or value < 0:
            raise MeshError(2, "receipt_metric")
    if rc == 0 and (not valid_hash(rec.get("artefact_sha256")) or rec["failed"]
                    or not all(valid_hash(rec[key]) for key in ("input_sha256", "policy_sha256", "toolchain_sha256"))):
        raise MeshError(2, "receipt_success")


def validate_success(rec, operation):
    """A schema-valid empty record cannot certify any geometry checks."""
    if rec["rc"] != 0:
        return
    if rec.get("artefact_sha256") != (rec["candidate_sha256"] or rec["input_sha256"]):
        raise MeshError(2, "success_artefact_binding")
    m = rec["metrics"]
    if not m["component_inventories"]:
        raise MeshError(2, "success_without_components")
    for inv in m["component_inventories"]:
        if (not isinstance(inv, dict) or type(inv.get("components")) is not int
                or inv["components"] < 1 or inv.get("failed") != []):
            raise MeshError(2, "success_without_topology")
    if operation in ("compare", "repair"):
        if (any(m[key] is None for key in METRICS) or not m["raw_diag_um"]
                or m.get("distance_is_exact") is not False
                or max(m["hausdorff_forward_um"], m["hausdorff_backward_um"]) * 1000000
                   > m["raw_diag_um"] * DISTANCE_MAX_PPM
                or not IOU_MIN_PPM <= m["silhouette_iou_ppm"] <= 1000000
                or not isinstance(m.get("silhouette_per_view_ppm"), dict)
                or set(m["silhouette_per_view_ppm"]) != {"front", "back", "right", "left"}
                or any(type(v) is not int or not IOU_MIN_PPM <= v <= 1000000
                       for v in m["silhouette_per_view_ppm"].values())):
            raise MeshError(2, "success_without_regression_evidence")
