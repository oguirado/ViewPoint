# SPDX-License-Identifier: AGPL-3.0-only
"""Resolve actor bindings, edit/source ranges, action anchors and screen direction."""
import math

from common import Parser, destination, dump, project, require, run, slot, valid_slug, value_digest

EDIT = "python3 -B blocking.py --project PROJECT --out BLOCKING"
EVENTS = ("first_step", "weight_stop", "reach", "causal_press", "release", "reply")


def refuse(condition, reason, fix):
    require(condition, reason, fix, EDIT)


def pair(value, low, high):
    return isinstance(value, list) and len(value) == 2 and all(type(v) is int and low <= v <= high for v in value)


def vector(value, length):
    return isinstance(value, list) and len(value) == length and all(
        type(v) in (float, int) and math.isfinite(v) for v in value)


def project_point(point, camera, pixels):
    """Map world axes (x along bench, y depth, z up) into a same-side clay camera."""
    dx, dy, dz = (a - b for a, b in zip(point, camera["target"]))
    yaw = math.radians(camera["theta"] - 50)
    elev = math.radians(camera["elev"])
    across = (dx * math.cos(yaw) + dy * math.sin(yaw)) * -camera["side"]
    depth = dy * math.cos(yaw) - dx * math.sin(yaw)
    scale = camera["scale"] * camera["lens"] / 40
    return (pixels[0] / 2 + across * scale,
            pixels[1] / 2 - (dz * math.cos(elev) + depth * math.sin(elev)) * scale)


def resolve(docs):
    brief = docs["brief"]
    upstream = docs["upstream"]
    plan = docs["blocking-plan"]
    assets = docs["clay-proxies"]["assets"]
    actors = brief["actors"]
    clock = upstream["episode"]
    matches_clock = (brief["fps"], brief["pixels"], clock["source_origin"], clock["edit_origin"]) == (
        clock["fps"], clock["pixels"], 1, 0)
    refuse(matches_clock, "unsupported_clock", "restore the declared editorial fixture clock")
    refuse(valid_slug(brief["id"]) and len(actors) == 2 and len({a["id"] for a in actors}) == 2,
           "invalid_cast", "declare two distinct actor IDs and a valid project ID")
    cast = {}
    for actor in actors:
        actor_id, asset_id = actor["id"], actor["asset"]
        asset = assets.get(asset_id, {})
        refuse(valid_slug(actor_id) and valid_slug(asset_id) and asset.get("rig") == actor["rig"],
               "wrong_actor", "restore the actor's bound clay asset and rig")
        cast[actor_id] = {**actor, "asset_sha256": value_digest(asset), "form": asset["form"]}
    refuse({a["form"] for a in cast.values()} == {"swivel", "cart"} and set(plan["actors"]) == set(cast),
           "wrong_actor", "bind one swivel performer and one cart to the blocking plan")
    cast_by_form = {a["form"]: actor_id for actor_id, a in cast.items()}
    for actor_id, position in plan["actors"].items():
        refuse(vector(position["start_m"], 2) and vector(position["stop_m"], 2),
               "invalid_blocking", "supply finite start and stop positions for each actor")
        refuse(position["gaze"] in cast and position["gaze"] != actor_id,
               "flipped_eyeline", "point each existing actor toward its scene partner")
    stage = brief["set"]
    refuse(all(vector(stage[name], 3) for name in ("switch_m", "signal_m")),
           "invalid_blocking", "supply finite positions for the switch and signal")
    swivel = plan["actors"][cast_by_form["swivel"]]
    cart = plan["actors"][cast_by_form["cart"]]
    sx, sy, sz = stage["switch_m"]
    stop_x, stop_y = swivel["stop_m"]
    refuse(swivel["start_m"][0] < stop_x < sx < cart["stop_m"][0]
           and math.dist((stop_x + 0.2, stop_y, 0.75), (sx, sy, sz)) <= 0.85,
           "unreachable_switch", "place the switch within reach after the planted stop")
    shots = brief["shots"]
    refuse(isinstance(shots, list) and shots and len({s["id"] for s in shots}) == len(shots)
           and set(plan["shot_cast"]) == {s["id"] for s in shots},
           "invalid_edit", "declare unique shots and supply a cast binding for each")
    cursor = 0
    resolved = []
    baseline = {actor_id: actor["asset"] for actor_id, actor in cast.items()}
    for shot in shots:
        refuse(plan["shot_cast"][shot["id"]] == baseline, "wrong_actor",
               "restore both canonical actor bindings in every shot")
        start, end = shot["edit"]
        before, after = shot["handles"]
        refuse(valid_slug(shot["id"]) and pair(shot["edit"], 0, 2400)
               and pair(shot["handles"], 0, 48) and start == cursor and start < end
               and type(shot["board_frame"]) is int and start <= shot["board_frame"] < end,
               "invalid_edit", "use contiguous, ordered, half-open edit ranges with a board frame inside each shot")
        camera = docs["cameras"]["presets"].get(shot["camera"])
        lighting = docs["lighting"]["presets"].get(shot["light"])
        refuse(camera is not None and lighting is not None, "missing_preset", "select existing camera and light presets")
        lens = upstream["layout"]["cameras"].get(camera["layout"])
        refuse(lens is not None and camera["side"] in (-1, 1) and vector(camera["target"], 3)
               and type(camera["scale"]) is int and 40 <= camera["scale"] <= 230,
               "invalid_camera", "bind the camera to a pinned layout lens and a valid placement")
        pose = {**camera, **lens}
        for key in ("wall_top", "wall_bottom", "floor", "shadow"):
            refuse(vector(lighting[key], 3) and all(type(v) is int and 0 <= v <= 255 for v in lighting[key]),
                   "invalid_light", "set light palette channels to RGB byte values")
        refuse(vector(lighting["key"], 3) and all(0 < v <= 2 for v in lighting["key"]),
               "invalid_light", "set positive light gains within the renderer range")
        for mark in ("start_m", "stop_m"):
            left, right = (project_point([*actor[mark], 0.6], pose, brief["pixels"])[0]
                           for actor in (swivel, cart))
            refuse(left < right and pose["side"] == -1, "flipped_eyeline",
                   "keep the camera on the approved side and preserve left-to-right actor order")
        resolved.append({**shot, "source": [1, end - start + before + after],
                         "cut": [1 + before, end - start + before],
                         "transition_in": "cut" if resolved else "start",
                         "camera_binding": pose, "lighting": lighting, "cast": cast})
        cursor = end
    events = brief["events"]
    refuse(all(type(events.get(n)) is int for n in EVENTS)
           and 0 <= events["first_step"] and all(events[a] < events[b] for a, b in zip(EVENTS, EVENTS[1:]))
           and events["reply"] < cursor, "invalid_action_order", "place the action marks in sequence on the edit clock")
    for element in upstream["performance"]["elements"]:
        if element["required"] and "after" in element:
            refuse(events[element["after"]] < events[element["id"]], "invalid_action_order",
                   "keep the supported stop before switch contact")
    return {"schema": "rp4.editorial-blocking/v1", "id": brief["id"], "title": brief["title"],
            "fps": brief["fps"], "pixels": brief["pixels"], "total_edit_frames": cursor,
            "events": events, "cast": cast, "cast_by_form": cast_by_form, "shots": resolved,
            "plan": plan["actors"], "set": stage, "assets": assets}


def smooth(value):
    value = min(1, max(0, value))
    return value * value * (3 - 2 * value)


def frame_state(block, frame):
    events = block["events"]
    switch = block["set"]["switch_m"]
    press = events["causal_press"]
    release = events["release"]
    approach = smooth(frame / events["weight_stop"])
    reach = smooth((frame - events["reach"]) / (press - events["reach"]))
    reach *= 1 - smooth((frame - release) / (events["reply"] - release))
    down = press <= frame < release
    actors = {}
    for actor_id, bind in block["cast"].items():
        plan = block["plan"][actor_id]
        root = [a + (b - a) * approach for a, b in zip(plan["start_m"], plan["stop_m"])]
        actors[actor_id] = {"root": root, "gaze": plan["gaze"], "asset_sha256": bind["asset_sha256"]}
    return {
        "frame": frame, "actors": actors, "reach": reach, "loaded": frame >= events["weight_stop"],
        "switch_z": switch[2] - (0.055 if down else 0), "contact": down,
        "signal": frame >= press, "reply": smooth((frame - events["reply"]) / 12),
    }


def main():
    parser = Parser(description=__doc__)
    slot(parser)
    args = parser.parse_args()
    block = resolve(project(args.project))
    with destination(args.out) as stage:
        dump(stage / "blocking.json", block)
    return {"status": "BLOCKED_SCENE_READY", "actors": list(block["cast"]), "shots": len(block["shots"])}


if __name__ == "__main__":
    raise SystemExit(run(main))
