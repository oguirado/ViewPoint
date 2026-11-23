# @module {"id":"wa.perceive.blender","layer":1,"owner":"PERCEIVE","deps":["wa.state.blender","wa.state.schema","wa.perceive.ax"],"book":"HB-WA-s1-perceive"}
"""Pure host conversion of a closed Blender snapshot into canonical STATE.

ORIGIN: base-state/extension separation in wender/perceive/godot.py.
Mesh data stays in an authenticated sidecar keyed by base node id. The base's
AX hashes still cover only prune_ax_v1 fields. An observation digest is not
render evidence: pixels and appearance are AUSENTE on this structured path.
There is no bpy import, frame capture, file publication or default byte budget.
"""
import copy
import hashlib

from wender.state import schema
from wender.state.blender import BlenderStateError, bounded_bytes, validate_scene
from . import ax


def _sha(value):
    return hashlib.sha256(schema.dumps(value).encode("utf-8")).hexdigest()


# @anchor:wa.perceive.blender.build_blender_state
def build_blender_state(scene, *, max_bytes):
    """Return {state, blender} without modifying the caller's integer scene.

    ``state`` is wender.state/v1. ``blender`` binds its canonical digest to the
    scene digest and maps emitted node ids to full, unique object names.
    Object collection order is irrelevant; mesh index order is significant.
    """
    validate_scene(scene)
    normalized = copy.deepcopy(scene)
    normalized["objects"].sort(key=lambda obj: obj["name"])
    raw = [{"nodeId": "scene", "role": {"value": "application"},
            "name": {"value": scene["scene_name"]},
            "childIds": [str(i) for i in range(len(normalized["objects"]))]}]
    for i, obj in enumerate(normalized["objects"]):
        raw.append({"nodeId": str(i), "role": {"value": "group"},
                    "name": {"value": obj["name"]}, "properties": [
                        {"name": "selected", "value": {"value": obj["selected"]}},
                        {"name": "hidden", "value": {"value": obj["hidden"]}},
                    ]})
    nodes = ax.prune(raw)
    state = schema.build_state(
        nodes, kind="blender", url=None, title=ax.normalize_name(scene["scene_name"]),
        ax_sha256=ax.snapshot_sha(nodes), ax_stable_sha256=ax.snapshot_sha(nodes, False),
        prune={"name": ax.PRUNE_NAME, "version": ax.PRUNE_VERSION}, raw_count=len(raw),
    )
    result = {"state": state, "blender": {
        "base_sha256": _sha(state), "scene_sha256": _sha(normalized), "scene": normalized,
        "objects": {node["id"]: obj["name"]
                    for node, obj in zip(state["nodes"][1:], normalized["objects"])},
    }}
    bounded_bytes(result, max_bytes)
    return result


# @anchor:wa.perceive.blender.validate_blender_state
def validate_blender_state(observation, *, max_bytes):
    """Authenticate the full binding, including reconstructed prune hashes."""
    bounded_bytes(observation, max_bytes)
    if not isinstance(observation, dict) or set(observation) != {"state", "blender"}:
        raise BlenderStateError("invalid observation keys")
    sidecar = observation["blender"]
    if not isinstance(sidecar, dict) or set(sidecar) != {"base_sha256", "scene_sha256", "scene", "objects"}:
        raise BlenderStateError("invalid Blender sidecar keys")
    try:
        errors = schema.validate_state(observation["state"])
    except (KeyError, TypeError, AttributeError) as exc:
        raise BlenderStateError("malformed base STATE") from exc
    if errors:
        raise BlenderStateError("; ".join(errors))
    expected = build_blender_state(sidecar["scene"], max_bytes=max_bytes)
    if bounded_bytes(expected, max_bytes) != bounded_bytes(observation, max_bytes):
        raise BlenderStateError("Blender observation binding mismatch", rc=4)
