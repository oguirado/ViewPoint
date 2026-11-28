# SPDX-License-Identifier: AGPL-3.0-only
"""Wire-specific outside-capsule adapters for the offline fixture profile."""

from pathlib import PurePosixPath
import re


class WireRefused(ValueError):
    pass


def require(condition):
    if not condition:
        raise WireRefused("INVALID_CHANNEL_WIRE")


def closed(value, *keys):
    return type(value) is dict and value.keys() == set(keys)


def name(value):
    return type(value) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value)


def command(call, wire):
    require(closed(wire, "format", "node") and wire["format"] == "gltf" and name(wire["node"]))
    return wire["node"], {"contract": "local_blender_cli", "status": "local_export"}


def factory_dispatch(call, wire):
    from viewpoint.handlers import LocalDryRun

    require(closed(wire, "schema", "request_id", "op", "context", "args", "dry_run")
            and wire["schema"] == "vp.control.req/v1" and wire["op"] == "vp_factory_call"
            and wire["request_id"] == call["request_id"] and wire["dry_run"] is True
            and type(wire["context"]) is dict)
    context = wire["context"]
    require(context.get("capsule_id") == call["capsule_id"]
            and context.get("incarnation") == call["incarnation"])
    reply = LocalDryRun().handle(wire)
    require(reply["schema"] == "vp.control.reply/v1" and reply["request_id"] == call["request_id"]
            and reply["code"] == "HOLD" and reply["ok"] is False
            and reply["evidence"] == []
            and "No stage executed and no checkpoint taken." in " ".join(reply["not_verified"]))
    # The retained scene represents the checkpoint intent of this dry-run.
    node = wire["args"]["op"].removeprefix("af.").replace(".", "_")
    require(name(node))
    return node, {"contract": reply["schema"], "status": reply["code"], "checkpoint_taken": False,
                  "effective_request_sha256": reply["effective_request_sha256"]}


def blender_gui(call, wire):
    from vqa3d.adapters.blender_live import validate_action

    require(closed(wire, "action", "node") and name(wire["node"])
            and type(wire["action"]) is dict)
    try:
        action = validate_action(wire["action"])
    except (ValueError, TypeError) as exc:
        raise WireRefused("INVALID_CHANNEL_WIRE") from exc
    require(action == {"op": "run_script", "path": "fixture_scene.py"})
    # This validates the trusted-script action shape without dispatching to a GUI.
    return wire["node"], {"contract": "blender_live", "status": "offline_stub", "action": action}


def _perception(call, wire, source, value, observation):
    from viewpoint.perception import Answer, PerceptionRefused, Probes, Scope, Target, route

    require(name(wire["view_id"]) and wire["ceiling"] == "accessibility"
            and closed(wire["target"], "capsule_id", "incarnation", "view_id"))
    admitted = Target(call["capsule_id"], call["incarnation"], wire["view_id"])
    observed = Target(**wire["target"])
    if observation is not None:
        require(closed(observation, "target", "value") and observation["target"] == wire["target"])
        require(closed(observation["value"], *value) and all(
            type(observation["value"][key]) is type(expected) and observation["value"][key] == expected
            for key, expected in value.items()
        ))
        observed, value = Target(**observation["target"]), observation["value"]
    probes = Probes(**{source: lambda target: Answer(observed, value)})
    try:
        result = route(Scope(admitted, "SOL-EXTERNAL-FIX", wire["ceiling"]),
                       "accessibility", nonvisual=False, probes=probes)
    except PerceptionRefused as exc:
        raise WireRefused(str(exc)) from exc
    require(result["source"] == source and result["level"] == "accessibility")
    return result["value"]


def cdp(call, wire, observation=None):
    require(closed(wire, "view_id", "ceiling", "target", "node") and
            closed(wire["node"], "backend_node_id", "name"))
    node = wire["node"]
    require(type(node["backend_node_id"]) is int and node["backend_node_id"] > 0 and name(node["name"]))
    value = _perception(call, wire, "cdp", node, observation)
    return value["name"], {"contract": "perception.Probes.cdp",
                           "status": "offline_stub" if observation is None else "local_observed",
                           "backend_node_id": value["backend_node_id"], "target": wire["target"]}


def at_spi(call, wire, observation=None):
    from wender.perceive.godot import parse_ui_state

    require(closed(wire, "view_id", "ceiling", "target", "accessible") and
            closed(wire["accessible"], "control_path", "ui_state", "name"))
    node = wire["accessible"]
    require(name(node["name"]) and type(node["control_path"]) is str)
    path = PurePosixPath(node["control_path"])
    require(path.is_absolute() and ".." not in path.parts and path.name == node["name"])
    try:
        state = parse_ui_state(node["ui_state"])
    except ValueError as exc:
        raise WireRefused("INVALID_CHANNEL_WIRE") from exc
    value = _perception(call, wire, "at_spi", node, observation)
    return value["name"], {"contract": "perception.Probes.at_spi",
                           "status": "offline_stub" if observation is None else "local_observed",
                           "control_path": value["control_path"], "state": state.family,
                           "target": wire["target"]}


CHANNEL_ADAPTERS = {
    "command": command,
    "factory_dispatch": factory_dispatch,
    "blender_gui": blender_gui,
    "cdp": cdp,
    "at_spi": at_spi,
}
