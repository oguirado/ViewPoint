"""Bounded integer sampling and relational obligations; no runtime imports."""
from math import isqrt
from .canon import integer, canonical_json


def frames_from_spec(spec, max_sample_frames=2000):
    if type(spec) is not list or len(spec) != 3:
        raise ValueError("frames_require_start_end_step")
    start, end, step = [integer(v) for v in spec]
    if step <= 0 or end < start:
        raise ValueError("invalid_frame_range")
    count = (end - start) // step + 1
    if integer(max_sample_frames) <= 0 or count > max_sample_frames:
        raise ValueError("frame_sample_limit")
    return list(range(start, end+1, step))


def validate_sample_request(frames, paths, max_sample_frames=2000, *, require_paths=True):
    if (type(frames) is not list or not 1 <= len(frames) <= integer(max_sample_frames) or
            any(type(f) is not int for f in frames)):
        raise ValueError("invalid_sample_frames")
    if type(paths) is not list or (require_paths and not paths) or any(type(p) is not str or not p for p in paths):
        raise ValueError("invalid_sample_paths")


def sample_adapter(adapter, frames, paths, max_sample_frames=2000):
    validate_sample_request(frames, paths, max_sample_frames, require_paths=False)
    if not paths:
        return {str(frame): {} for frame in frames}
    method = getattr(adapter, "sample", None)
    if callable(method):
        return method(frames, paths)
    from .check import lookup
    samples = {}
    # Restore the observation frame even on an invalid sample.
    original = adapter.scene_state().get("frame_current")
    try:
        for frame in frames:
            result = adapter.act({"op": "set_frame", "frame": frame})
            if result.get("ok") is not True:
                raise ValueError("sample_action_failed")
            state = adapter.scene_state()
            samples[str(frame)] = {p: lookup(state, p) for p in paths}
    finally:
        if type(original) is int:
            restored = adapter.act({"op": "set_frame", "frame": original})
            if restored.get("ok") is not True:
                raise ValueError("sample_restore_failed")
    return samples


def vector(value):
    if type(value) is not list or len(value) != 3:
        raise ValueError("distance_requires_three_integers")
    return [integer(v) for v in value]


def values(value):
    return [integer(v) for v in value] if type(value) is list and value else [integer(value)]


def distance_check(a, b, maximum, suffix=""):
    squared = sum((x-y)**2 for x, y in zip(vector(a), vector(b)))
    distance = isqrt(squared)
    if squared > maximum**2:
        return "FAIL", f"distance_exceeded{suffix}:distance_um={distance}:max_um={maximum}"
    return "PASS", f"within_distance{suffix}:distance_um={distance}"


def evaluate_temporal(adapter, state, obligation):
    from .check import lookup
    kind = obligation["kind"]
    if kind == "within_distance" and "frames" not in obligation:
        points = [lookup(state, obligation[k]) if type(obligation[k]) is str else obligation[k]
                  for k in ("a", "b")]
        return distance_check(*points, obligation["max_um"])
    maximum = obligation.get("max_sample_frames", 2000)
    frames = obligation["frames"] if kind == "within_distance" else frames_from_spec(obligation["frames"], maximum)
    validate_sample_request(frames, [], maximum, require_paths=False)
    paths = ([obligation["path"]] if kind != "within_distance" else
             list(dict.fromkeys(obligation[k] for k in ("a", "b") if type(obligation[k]) is str)))
    samples = sample_adapter(adapter, frames, paths, maximum)
    samples = {str(k): v for k, v in samples.items()}
    canonical_json(samples)
    previous = previous_frame = None
    distances = []
    largest_delta, largest_pair = -1, None
    for frame in frames:
        row = samples[frame] if frame in samples else samples[str(frame)]
        if kind == "within_distance":
            points = [row[obligation[k]] if type(obligation[k]) is str else obligation[k] for k in ("a", "b")]
            verdict, reason = distance_check(*points, obligation["max_um"], f":frame={frame}")
            if verdict == "FAIL":
                return verdict, reason
            distances.append([frame, isqrt(sum((x-y)**2 for x, y in zip(*points)))])
        else:
            current = values(row[obligation["path"]])
            if kind == "range_over_frames":
                for index, value in enumerate(current):
                    if (("min" in obligation and value < obligation["min"]) or
                            ("max" in obligation and value > obligation["max"])):
                        return "FAIL", f"range_exceeded:{obligation['path']}:frame={frame}:element={index}:value={value}"
            elif previous is not None:
                if len(previous) != len(current):
                    raise ValueError("sample_vector_length_changed")
                delta = max(abs(x-y) for x, y in zip(previous, current))
                if delta > largest_delta:
                    largest_delta, largest_pair = delta, (previous_frame, frame)
            previous, previous_frame = current, frame
    if kind == "max_delta_over_frames" and largest_delta > obligation["max_delta"]:
        return "FAIL", f"max_delta_exceeded:{obligation['path']}:frames={largest_pair[0]},{largest_pair[1]}:delta={largest_delta}"
    return "PASS", ("distances_um:" + canonical_json(distances) if distances else "frame_samples_within_bounds")


def capability_check(probe, obligation):
    for key, wanted in obligation.items():
        if key == "kind":
            continue
        if key == "engine":
            present = wanted in probe.get("engines", [])
        elif key == "device_type":
            present = bool(probe.get("cycles_devices", {}).get(wanted))
            if wanted != "CPU":
                present = present and probe.get("gpu_render_ok_by_type", {}).get(wanted) is True
        elif key == "denoiser":
            present = wanted in probe.get("denoisers", [])
        elif key == "view_transform":
            present = wanted in probe.get("view_transforms", [])
        else:
            looks = probe.get("looks", {})
            transform = obligation.get("view_transform")
            present = wanted in (looks.get(transform, []) if transform else
                                 [look for group in looks.values() for look in group])
        if not present:
            return "CANNOT_SEE", f"capability_missing:{key}:{wanted}"
    return "PASS", "capability_available"
