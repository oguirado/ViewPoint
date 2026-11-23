"""Discrete joint and skinned-sole measurements for the ACT fixture."""
from collections import Counter, defaultdict
from fractions import Fraction
import hashlib
import json
import math
import time

from asset_factory import core
from asset_factory.domains import UM_PER_METER
from providers.unirig.common import Refusal, require
from providers.unirig.gate import mul, node_matrix, point

from . import action

LIMITS = (
    "joint_displacement_um",
    "joint_rest_displacement_um",
    "joint_speed_um_s",
    "rotation_rate_upper_urad_s",
    "stance_slide_upper_um",
    "plant_speed_upper_um_s",
    "stance_gap_upper_um",
    "floor_clearance_lower_um",
)
REST_LIMITS = {
    "rest_floor_clearance_lower_um": "floor_clearance_lower_um",
    "rest_stance_gap_upper_um": "stance_gap_upper_um",
}
MAX_STANCE_GAP_UM = 1_000
MAX_FRAMES = 2_401
MAX_INPUT_SECONDS = 60
FEET = ("left_foot", "right_foot")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _rational(pair):
    require(type(pair) in (tuple, list) and len(pair) == 2 and
            all(type(v) is int and 0 <= v <= 10**9 for v in pair) and pair[1] > 0,
            2, "rational_time_invalid")
    return Fraction(*pair)


def _pair(value):
    return [value.numerator, value.denominator]


def _v3(row):
    return type(row) is list and len(row) == 3 and all(type(v) is int for v in row)


def _registered_plan(plan, pin, skin_data, anatomy_data, receipt_data):
    expected = {"schema", "name", "duration", "skin_sha256", "anatomy_sha256",
                "skin_receipt_sha256", "frame", "support_plane_sha256", "soles",
                "stances", "required_channels", "limits"}
    require(not (type(plan) is dict and plan.get("schema") == "af.motion-registration/v1"),
            3, "support_plane_registration_required")
    require(type(plan) is dict and set(plan) == expected and
            plan["schema"] == "af.motion-registration/v2", 2, "registration_invalid")
    require(core.sha256_json(plan) == pin, 4, "registration_stale")
    for name, source in (("skin", skin_data), ("anatomy", anatomy_data), ("skin_receipt", receipt_data)):
        require(type(source) is bytes and source, 3, name + "_unavailable")
        require(len(source) <= action.MAX_ACTION_BYTES, 6, name + "_byte_budget")
        require(plan[name + "_sha256"] == sha256(source), 4, name + "_stale")
    anatomy, upstream = json.loads(anatomy_data), json.loads(receipt_data)
    require(upstream["schema"] == "af.skin-gate/v1" and upstream["rc"] == 0 and
            upstream["verdict"] == "PASS" and upstream["scope"] == "independent-fixture-discrete-exact-LBS"
            and upstream["rigged_sha256"] == plan["skin_sha256"] and
            upstream["fixture_digest"] == "sha256:" + plan["anatomy_sha256"],
            3, "accepted_skin_required")
    require(plan["frame"] == anatomy["frame"] == "gltf_Y_up_right_handed",
            2, "support_frame_invalid")
    require(type(plan["name"]) is str and bool(plan["name"]), 2, "action_name_invalid")
    channels = plan["required_channels"]
    require(type(channels) is dict and channels and
            all(name in anatomy["joint_origins"] and type(path) is str and path in action.WIDTH
                for name, path in channels.items()), 2, "required_channels_invalid")
    limits = plan["limits"]
    require(type(limits) is dict and set(limits) == set(LIMITS) | {"contact_tolerance_um"} and
            all(type(v) is int and (v < 0 if k == "floor_clearance_lower_um" else v > 0)
                for k, v in limits.items()), 2, "fixture_limits_invalid")
    require(limits["stance_gap_upper_um"] <= MAX_STANCE_GAP_UM, 2, "stance_gap_limit_invalid")
    duration = _rational(plan["duration"])
    require(0 < duration <= MAX_INPUT_SECONDS, 6, "duration_budget")
    require(set(plan["soles"]) == set(plan["stances"]) == set(FEET), 3, "sole_registration_missing")
    stances = {}
    for foot in FEET:
        intervals = [tuple(map(_rational, interval)) for interval in plan["stances"][foot]]
        require(intervals and all(0 <= a < b <= duration for a, b in intervals) and
                all(a[1] < b[0] for a, b in zip(intervals, intervals[1:])),
                2, "stance_intervals_invalid")
        stances[foot] = intervals
    return anatomy, duration, stances


class _SupportPlane:
    def __init__(self, source, plan):
        require(type(source) is bytes and source, 3, "support_plane_unavailable")
        require(len(source) <= action.MAX_ACTION_BYTES, 6, "support_plane_byte_budget")
        require(sha256(source) == plan["support_plane_sha256"], 4, "support_plane_stale")
        plane = json.loads(source)
        require(type(plane) is dict and set(plane) == {"schema", "frame", "point_um", "normal"} and
                plane["schema"] == "af.motion-support-plane/v1" and
                core.canonical_json(plane).encode("utf-8") == source and
                _v3(plane["point_um"]) and _v3(plane["normal"]), 2, "support_plane_invalid")
        require(plane["frame"] == plan["frame"], 5, "support_plane_frame_mismatch")
        nx, ny, nz = plane["normal"]
        # TARGET_UNCALIBRATED: in the Y-up frame tan(30°)^2 = 1/3.
        require(ny > 0 and 3 * (nx * nx + nz * nz) <= ny * ny, 2, "support_normal_invalid")
        normal = (nx, ny, nz)
        length = math.hypot(*normal)
        require(0 < length < math.inf, 2, "support_normal_invalid")
        self.point_um = plane["point_um"]
        self.normal = tuple(x / length for x in normal)

    def height(self, p):
        return math.fsum((coordinate * UM_PER_METER - offset) * normal
                         for coordinate, offset, normal in zip(p, self.point_um, self.normal))

    def tangential_distance(self, a, b):
        x, y, z = (p - q for p, q in zip(a, b))
        nx, ny, nz = self.normal
        return math.hypot(y*nz - z*ny, z*nx - x*nz, x*ny - y*nx) * UM_PER_METER


class _Fixture:
    def __init__(self, skin, anatomy, plan):
        self.skin = skin
        self.nodes = skin.doc["nodes"]
        self.joints = skin.doc["skins"][0]["joints"]
        self.names = {self.nodes[index]["name"]: index for index in self.joints}
        require(len(self.names) == len(self.joints) and self.names.keys() == anatomy["joint_origins"].keys(),
                5, "registered_joints_mismatch")
        self.parents, self.rest = skin.hierarchy()
        self.order = sorted(range(len(self.nodes)), key=self._depth)
        self.rotations = [action.unit(node.get("rotation", action.IDENTITY_ROTATION))
                          for node in self.nodes]
        require(all("matrix" not in node and node.get("scale", [1, 1, 1]) == [1, 1, 1]
                    for node in self.nodes), 3, "rigid_trs_fixture_required")
        require(all(_um_point(self.rest[index][12:15]) == anatomy["joint_origins"][name]
                    for name, index in self.names.items()), 5, "registered_joint_origin_mismatch")

        (mesh_node,) = (node for node in self.nodes if "mesh" in node)
        (primitive,) = skin.doc["meshes"][mesh_node["mesh"]]["primitives"]
        require(mesh_node["skin"] == 0 and primitive.get("mode", 4) == 4 and
                not primitive.get("targets"), 3, "connected_skin_required")
        attrs = primitive["attributes"]
        require(not any(k.startswith(("JOINTS_", "WEIGHTS_")) and k not in ("JOINTS_0", "WEIGHTS_0")
                        for k in attrs), 3, "extra_influences_unsupported")
        positions, _ = skin.accessor(attrs["POSITION"], "VEC3", (5126,))
        vertices_um = [_um_point(p) for p in positions]
        require(Counter(map(tuple, vertices_um)) == Counter(map(tuple, anatomy["vertices_um"])),
                5, "registered_surface_mismatch")
        indexes = defaultdict(list)
        for i, position in enumerate(vertices_um):
            indexes[tuple(position)].append(i)

        indices, _ = skin.accessor(primitive["indices"], "SCALAR", (5121, 5123, 5125))
        triangles = Counter(frozenset(row[0] for row in indices[n:n+3])
                            for n in range(0, len(indices), 3))
        indices_used = set()
        sole_triangles = set()
        self.soles = {}
        for foot in FEET:
            triangle_points = plan["soles"][foot]
            require(type(triangle_points) is list and bool(triangle_points), 3, "sole_triangles_missing")
            registered = []
            for triangle in triangle_points:
                require(type(triangle) is list and len(triangle) == 3 and all(_v3(p) for p in triangle),
                        2, "sole_triangle_invalid")
                require(all(tuple(p) in indexes for p in triangle), 5, "sole_vertex_unregistered")
                require(all(len(indexes[tuple(p)]) == 1 for p in triangle), 3, "ambiguous_sole_vertex")
                face = frozenset(indexes[tuple(p)][0] for p in triangle)
                require(len(face) == 3 and triangles[face] == 1 and face not in sole_triangles,
                        5, "sole_triangle_unregistered")
                sole_triangles.add(face)
                registered.extend(face)
            foot_indices = sorted(set(registered))
            require(not indices_used.intersection(foot_indices), 5, "sole_patches_overlap")
            indices_used.update(foot_indices)
            self.soles[foot] = foot_indices
        require(sole_triangles == {t for t in triangles if t <= indices_used},
                3, "sole_coverage_missing")

        inverse_bind, _ = skin.accessor(skin.doc["skins"][0]["inverseBindMatrices"], "MAT4", (5126,))
        weights, _ = skin.accessor(attrs["WEIGHTS_0"], "VEC4", (5126, 5121, 5123))
        slots, _ = skin.accessor(attrs["JOINTS_0"], "VEC4", (5121, 5123))
        require(len(inverse_bind) == len(self.joints) and len(weights) == len(slots) == len(positions),
                5, "sole_skin_count")
        self.palette = list(zip(self.joints, inverse_bind))
        self.positions = positions
        self.influences = {}
        for foot, patch in self.soles.items():
            for i in patch:
                require(all(0 <= j < len(self.joints) for j in slots[i]) and
                        all(w >= 0 for w in weights[i]) and abs(math.fsum(weights[i]) - 1) <= 1e-6,
                        5, "sole_skin_invalid")
                require(sum(w for j, w in zip(slots[i], weights[i])
                            if self.joints[j] == self.names[foot]) > 0,
                        5, "sole_foot_binding_mismatch")
                self.influences[i] = [(j, w) for j, w in zip(slots[i], weights[i]) if w]

    def _depth(self, index):
        depth = 0
        while self.parents[index] is not None:
            index = self.parents[index]
            depth += 1
        return depth

    def pose(self, tracks, pts):
        sampled = {key: track.sample(pts) for key, track in tracks.items()}
        world, rotations = {}, {}
        for index in self.order:
            rest = self.nodes[index]
            rotation = sampled.get((index, "rotation"), self.rotations[index])
            translation = sampled.get((index, "translation"), rest.get("translation", (0, 0, 0)))
            local = node_matrix({"rotation": list(rotation), "translation": list(translation)})
            parent = self.parents[index]
            world[index] = local if parent is None else mul(world[parent], local)
            rotations[index] = rotation if parent is None else action.unit(
                action.quaternion_product(rotations[parent], rotation))
        palette = [mul(world[index], bind) for index, bind in self.palette]
        origins = {name: world[index][12:15] for name, index in self.names.items()}
        soles = {}
        for foot, patch in self.soles.items():
            positions = []
            for index in patch:
                moved = [(point(palette[joint], self.positions[index]), weight)
                         for joint, weight in self.influences[index]]
                positions.append(tuple(math.fsum(p[k]*w for p, w in moved) for k in range(3)))
            soles[foot] = positions
        return origins, {name: rotations[index] for name, index in self.names.items()}, soles


def _um_point(point):
    return [round(x * UM_PER_METER) for x in point]


def _distance(a, b):
    return math.dist(a, b) * UM_PER_METER


def _sample(fixture, tracks, frame_count, step, duration, stances, limits, support, deadline_ns):
    maxima = dict.fromkeys(LIMITS, 0)
    contact_mismatches = 0
    frame_numbers = {frame * step: frame for frame in range(frame_count)}
    key_times = {pts for track in tracks.values() for pts in track.times if 0 < pts <= duration}
    trace_frames, trace_keys, timings = [], [], []
    anchor, last_plant = {}, {}
    last_pts = last_origins = last_rotations = last_frame_origins = None
    clearance_min = math.inf
    rest_origins, _, rest_soles = fixture.pose({}, Fraction(0))
    rest_heights = [support.height(p) for sole in rest_soles.values() for p in sole]
    phase_samples = {foot: Counter() for foot in FEET}
    for pts in sorted(frame_numbers.keys() | key_times):
        started = time.monotonic_ns()
        require(started <= deadline_ns, 6, "deadline_exceeded")
        frame = frame_numbers.get(pts)
        interval_seconds = None if last_pts is None else float(pts - last_pts)
        origins, rotations, soles = fixture.pose(tracks, pts)
        joint_movement = {}
        for name, origin in origins.items():
            rest_delta = math.ceil(_distance(origin, rest_origins[name]))
            maxima["joint_rest_displacement_um"] = max(maxima["joint_rest_displacement_um"], rest_delta)
            if frame is not None and last_frame_origins is not None:
                frame_delta = math.ceil(_distance(origin, last_frame_origins[name]))
                maxima["joint_displacement_um"] = max(maxima["joint_displacement_um"], frame_delta)
            if last_origins is not None:
                delta = _distance(origin, last_origins[name])
                speed = math.ceil(delta / interval_seconds)
                angular = math.ceil(action.relative_angle(last_rotations[name], rotations[name])
                                     * 1_000_000 / interval_seconds)
                maxima["joint_speed_um_s"] = max(maxima["joint_speed_um_s"], speed)
                maxima["rotation_rate_upper_urad_s"] = max(maxima["rotation_rate_upper_urad_s"], angular)
                joint_movement[name] = [math.ceil(delta), speed, angular]
        last_pts, last_origins, last_rotations = pts, origins, rotations
        if frame is not None:
            last_frame_origins = origins
        sole_samples = {}
        for foot, patch in soles.items():
            heights = [support.height(p) for p in patch]
            clear = math.floor(min(heights))
            clearance_min = min(clearance_min, clear)
            contact = abs(min(heights)) <= limits["contact_tolerance_um"]
            interval = next((i for i, (a, b) in enumerate(stances[foot]) if a <= pts <= b), None)
            expected = interval is not None
            if frame is not None:
                phase_samples[foot][interval] += 1
            contact_mismatches += (contact != expected)
            slide = plant_speed = gap = None
            if expected:
                if foot not in anchor or anchor[foot][0] != interval:
                    anchor[foot] = (interval, patch)
                slide = math.ceil(max(support.tangential_distance(a, b)
                                      for a, b in zip(anchor[foot][1], patch)))
                maxima["stance_slide_upper_um"] = max(maxima["stance_slide_upper_um"], slide)
                gap = max(0, math.ceil(max(heights)))
                maxima["stance_gap_upper_um"] = max(maxima["stance_gap_upper_um"], gap)
                if foot in last_plant and last_plant[foot][0] == interval:
                    dx = max(support.tangential_distance(a, b)
                             for a, b in zip(last_plant[foot][1], patch))
                    plant_speed = math.ceil(dx / interval_seconds)
                    maxima["plant_speed_upper_um_s"] = max(maxima["plant_speed_upper_um_s"], plant_speed)
                last_plant[foot] = (interval, patch)
            else:
                anchor.pop(foot, None)
                last_plant.pop(foot, None)
            sole_samples[foot] = {"points_um": [_um_point(p) for p in patch],
                                  "clearance_um": clear, "expected_contact": expected, "contact": contact,
                                   "stance_slide_um": slide, "plant_speed_um_s": plant_speed,
                                   "stance_gap_um": gap}
        row = {"pts": _pair(pts), "joints": {
            name: _um_point(p) for name, p in origins.items()},
            "joint_deltas": joint_movement, "soles": sole_samples}
        if frame is None:
            trace_keys.append(row)
        else:
            trace_frames.append(row | {"frame_index": frame})
            timings.append(time.monotonic_ns() - started)
    require(all(phase_samples[foot][None] and
                all(phase_samples[foot][i] >= 2 for i in range(len(stances[foot])))
                for foot in FEET), 3, "contact_phase_coverage_missing")
    require(time.monotonic_ns() <= deadline_ns, 6, "deadline_exceeded")
    maxima["floor_clearance_lower_um"] = clearance_min
    maxima["rest_floor_clearance_lower_um"] = math.floor(min(rest_heights))
    maxima["rest_stance_gap_upper_um"] = max(0, math.ceil(max(rest_heights)))
    maxima["contact_mismatches"] = contact_mismatches
    return maxima, trace_frames, trace_keys, timings


def evaluate(data, skin_data, anatomy_data, skin_receipt_data, plan, registration_sha256,
             fps, *, support_data=None, wall_budget_ns=5_000_000_000):
    """Return a fixture-only receipt and, when complete, its canonical sample trace."""
    started = time.monotonic_ns()
    receipt = {
        "schema": "af.motion-receipt/v1", "scope": "fixture-output-frame-and-decoded-key-samples",
        "calibration_state": "TARGET_UNCALIBRATED", "production_verdict": "CANNOT_SEE",
        "rc": 2, "status": "ERROR", "verdict": "ERROR", "reason_codes": [],
        "action_sha256": None, "skin_sha256": None, "anatomy_sha256": None,
        "skin_receipt_sha256": None, "registration_sha256": None, "support_plane_sha256": None,
        "trace_sha256": None,
        "coverage": None, "limits": None, "metrics": None, "cost": None, "wall_ns": 0,
    }
    trace = None
    try:
        require(type(wall_budget_ns) is int and wall_budget_ns > 0, 2, "wall_budget_invalid")
        deadline_ns = started + wall_budget_ns
        require(type(data) is bytes and data, 3, "action_unavailable")
        require(len(data) <= action.MAX_ACTION_BYTES, 6, "action_byte_budget")
        receipt["action_sha256"] = sha256(data)
        anatomy, duration, stances = _registered_plan(plan, registration_sha256, skin_data,
                                                     anatomy_data, skin_receipt_data)
        for key in ("skin_sha256", "anatomy_sha256", "skin_receipt_sha256"):
            receipt[key] = plan[key]
        receipt["registration_sha256"] = registration_sha256
        frame_rate = _rational(fps)
        require(frame_rate > 0, 2, "output_grid_invalid")
        frame_count = int(duration * frame_rate) + 1
        require(3 <= frame_count <= MAX_FRAMES, 6, "output_frame_budget")
        support = _SupportPlane(support_data, plan)
        receipt["support_plane_sha256"] = plan["support_plane_sha256"]
        skin = action.open_glb(skin_data, deadline_ns)
        fixture = _Fixture(skin, anatomy, plan)
        tracks = action.decode(data, skin, name=plan["name"], duration=duration, deadline_ns=deadline_ns)
        require(all((fixture.names[name], path) in tracks
                    for name, path in plan["required_channels"].items()), 3, "required_channel_missing")
        limits = plan["limits"]
        metrics, frames, keys, timings = _sample(fixture, tracks, frame_count, 1 / frame_rate,
                                                 duration, stances, limits, support, deadline_ns)
        failures = []
        for metric_key in (*LIMITS, *REST_LIMITS):
            limit_key = REST_LIMITS.get(metric_key, metric_key)
            limit = limits[limit_key]
            value = metrics[metric_key]
            exceeded = value < limit if limit_key == "floor_clearance_lower_um" else value > limit
            if exceeded:
                failures.append(metric_key)
        if metrics["contact_mismatches"]:
            failures.append("contact_schedule")

        trace = {"schema": "af.motion-fixture-trace/v1", "action_sha256": receipt["action_sha256"],
                 "registration_sha256": registration_sha256,
                 "support_plane_sha256": receipt["support_plane_sha256"], "fps": _pair(frame_rate),
                 "frames": frames, "key_samples": keys}
        trace_sha256 = core.sha256_json(trace)
        require(time.monotonic_ns() <= deadline_ns, 6, "deadline_exceeded")
        receipt.update(trace_sha256=trace_sha256, limits=limits, metrics=metrics,
                       coverage={"fps": _pair(frame_rate), "duration": _pair(duration),
                                 "expected_frames": frame_count, "measured_frames": len(frames),
                                  "additional_key_samples": len(keys),
                                 "joint_count": len(fixture.joints),
                                 "sole_vertices": sum(map(len, fixture.soles.values()))},
                       cost={"action_glb_bytes": len(data), "replayed_frames": len(timings),
                             "median_frame_ns": sorted(timings)[len(timings)//2],
                             "max_frame_ns": max(timings)})
        rc, reasons = (5, failures) if failures else (0, [])
    except Refusal as exc:
        rc, reasons = exc.rc, [exc.code]
        trace = None
    except (core.ContractError, KeyError, TypeError, ValueError, IndexError, AttributeError,
            OverflowError, RecursionError):
        rc, reasons = 2, ["motion_payload_invalid"]
        trace = None
    receipt.update(rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc],
                   reason_codes=reasons, wall_ns=time.monotonic_ns() - started)
    return receipt, trace
