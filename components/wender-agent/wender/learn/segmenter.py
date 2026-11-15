# @module {"id":"wa.learn.segmenter","layer":2,"owner":"LEARN","deps":["wa.learn.demonstrations","wa.remember.skills","wa.remember.program_map"],"book":"HB-WA-s4-learn"}
"""@module wa.learn.segmenter - deterministic in-RAM frame segmenter and demonstration pipeline.

Segments consecutive RGB24 frames in memory using integer tile-diff counts from an injected
vision engine (without importing vision_adapter). Runs injectable labeler, mapper, and replayer
stages per segment and computes reproduced_share_ppm via wender.learn.demonstrations.
STORAGE LAW: frames live only in RAM as bytes objects and never touch disk.
"""
import copy
import hashlib
import inspect

from . import demonstrations
from ..remember import skills
from ..remember.program_map import canonical_json

VALID_ACTION_PATHS = ("api", "ax", "coordinates")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _frame_fields(frame):
    if isinstance(frame, dict):
        width = frame.get("width")
        height = frame.get("height")
        data = frame.get("data")
    else:
        width = getattr(frame, "width", None)
        height = getattr(frame, "height", None)
        data = getattr(frame, "data", None)
    if not _is_int(width) or not _is_int(height) or width <= 0 or height <= 0:
        raise ValueError("frame width and height must be positive integers")
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("frame data must be RGB24 bytes in RAM")
    if len(data) != width * height * 3:
        raise ValueError("frame data length must equal width * height * 3")
    return width, height, data


class _TileDiffEngine:
    """Private stdlib tile-diff fake engine over in-RAM RGB24 frames for tests."""

    def __init__(self, tile=8, threshold=1):
        if not _is_int(tile) or tile <= 0:
            raise ValueError("tile must be a positive integer")
        if not _is_int(threshold) or threshold < 0:
            raise ValueError("threshold must be a non-negative integer")
        self.tile = tile
        self.threshold = threshold

    def changed_regions(self, prev, cur):
        pw, ph, pdata = _frame_fields(prev)
        cw, ch, cdata = _frame_fields(cur)
        if (pw, ph) != (cw, ch):
            raise ValueError("frame dimensions must match")
        stride = pw * 3
        cutoff = 1 if self.threshold <= 0 else self.threshold
        regions = []
        for y0 in range(0, ph, self.tile):
            th = min(self.tile, ph - y0)
            for x0 in range(0, pw, self.tile):
                tw = min(self.tile, pw - x0)
                changed = False
                for row in range(y0, y0 + th):
                    start = row * stride + x0 * 3
                    end = start + tw * 3
                    pslice = pdata[start:end]
                    cslice = cdata[start:end]
                    if pslice == cslice:
                        continue
                    if cutoff <= 1 or any(abs(a - b) >= cutoff for a, b in zip(pslice, cslice)):
                        changed = True
                        break
                if changed:
                    regions.append({"x": x0, "y": y0, "w": tw, "h": th, "tiles": 1})
        return regions


def _normalize_regions(raw_regions):
    if raw_regions is None:
        return [], 0
    regions = []
    total_tiles = 0
    for r in raw_regions:
        if not isinstance(r, dict):
            raise ValueError("region must be a dict")
        x, y, w, h, tiles = r.get("x"), r.get("y"), r.get("w"), r.get("h"), r.get("tiles")
        if not all(_is_int(v) for v in (x, y, w, h, tiles)):
            raise ValueError("region x, y, w, h and tiles must be integers")
        if w < 0 or h < 0 or tiles < 0:
            raise ValueError("region dimensions and tiles must be non-negative")
        total_tiles += tiles
        if tiles > 0 and w > 0 and h > 0:
            regions.append({"x": x, "y": y, "w": w, "h": h, "tiles": tiles})
    return regions, total_tiles


def _union_bbox(pair_entries):
    boxes = [r for _, _, regs in pair_entries for r in regs]
    if not boxes:
        return {"x": 0, "y": 0, "w": 0, "h": 0}
    x0 = min(r["x"] for r in boxes)
    y0 = min(r["y"] for r in boxes)
    x1 = max(r["x"] + r["w"] for r in boxes)
    y1 = max(r["y"] + r["h"] for r in boxes)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _finalize_segment(active_pairs, min_len):
    if len(active_pairs) < min_len:
        return None
    start = active_pairs[0][0]
    end = active_pairs[-1][0] + 1
    peak_tiles = max(count for _, count, _ in active_pairs)
    total_tiles = sum(count for _, count, _ in active_pairs)
    return {
        "start": start,
        "end": end,
        "peak_tiles": peak_tiles,
        "total_tiles": total_tiles,
        "bbox": _union_bbox(active_pairs),
    }


# @anchor:wa.learn.segmenter.segment_frames
def segment_frames(frames, engine, hi=4, lo=1, quiet=3, min_len=2):
    """Segment an in-RAM frame sequence by hysteresis on changed tile counts.

    Opens a segment when a consecutive pair has at least `hi` changed tiles, closes it after
    `quiet` consecutive pairs with at most `lo` changed tiles, and drops segments shorter than
    `min_len` pairs. Returns deterministic integer-only segment dicts.
    """
    for name, val, low in (("hi", hi, 1), ("lo", lo, 0), ("quiet", quiet, 1), ("min_len", min_len, 1)):
        if not _is_int(val) or val < low:
            raise ValueError("%s must be an integer >= %d" % (name, low))
    if lo > hi:
        raise ValueError("lo must be <= hi")

    seq = list(frames)
    if len(seq) < 2:
        return []

    segments = []
    open_pairs = None
    quiet_run = 0

    for idx in range(len(seq) - 1):
        regions, count = _normalize_regions(engine.changed_regions(seq[idx], seq[idx + 1]))
        if open_pairs is None:
            if count >= hi:
                open_pairs = [(idx, count, regions)]
                quiet_run = 0
        else:
            open_pairs.append((idx, count, regions))
            if count <= lo:
                quiet_run += 1
                if quiet_run >= quiet:
                    active = open_pairs[:-quiet]
                    seg = _finalize_segment(active, min_len)
                    if seg is not None:
                        segments.append(seg)
                    open_pairs = None
                    quiet_run = 0
            else:
                quiet_run = 0

    if open_pairs is not None:
        active = open_pairs[:-quiet_run] if quiet_run else open_pairs
        seg = _finalize_segment(active, min_len)
        if seg is not None:
            segments.append(seg)

    return segments


def _call_labeler(labeler, seg_copy, seg_frames):
    try:
        sig = inspect.signature(labeler)
        params = [
            p for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
        if not has_varargs and len(params) == 1:
            return labeler(seg_frames)
    except (ValueError, TypeError):
        pass
    return labeler(seg_copy, seg_frames)


def _validate_actions(actions):
    if isinstance(actions, dict):
        items = [actions]
    elif isinstance(actions, (list, tuple)) and actions:
        items = list(actions)
    else:
        raise ValueError("mapper must return non-empty structured actions")
    for act in items:
        if not isinstance(act, dict) or act.get("path") not in VALID_ACTION_PATHS:
            raise ValueError("each structured action must be a dict with path in api, ax or coordinates")
    return items if isinstance(actions, tuple) else actions


def _metadata_copy(value):
    """Detach integer-only, nonpixel JSON metadata from injected stage owners."""
    canonical_json(value)
    return copy.deepcopy(value)


def _verified_handoff(intent, actions, segment, receipt, replayer):
    """Translate the executed verifier's receipt to a complete legacy skill.

    A checkpoint receipt describes the verifier result. It is not a fabricated
    STATE predicate or C-11 support count; replay requires explicit expectations.
    """
    evidence = _metadata_copy(receipt.get("evidence", {}))
    if not isinstance(evidence, dict):
        raise ValueError("replayer evidence must be a dict")
    if "reproduced" in evidence and evidence["reproduced"] is not True:
        raise ValueError("replayer evidence contradicts reproduced result")
    if "verified" in evidence and evidence["verified"] is not True:
        raise ValueError("replayer evidence contradicts verified result")
    named = [holder["verified_by"] for holder in (receipt, evidence) if "verified_by" in holder]
    if named:
        if any(not isinstance(name, str) or not name.strip() for name in named) or len(set(named)) != 1:
            raise ValueError("replayer verifier lineage is missing or contradictory")
        verifier = named[0]
    else:
        # The actual executed injected callable is the only available lineage.
        module = getattr(replayer, "__module__", type(replayer).__module__)
        name = getattr(replayer, "__qualname__", type(replayer).__qualname__)
        verifier = module + "." + name
    evidence.update({"reproduced": True, "verified_by": verifier})
    evidence.setdefault("version", 1)
    if type(evidence["version"]) is not int or evidence["version"] < 1:
        raise ValueError("replayer evidence.version must be a positive exact integer")
    if "runs" in evidence and (type(evidence["runs"]) is not int or evidence["runs"] < 1):
        raise ValueError("replayer evidence.runs must be a positive exact integer")
    if "sha256" in evidence and (not isinstance(evidence["sha256"], str)
            or len(evidence["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in evidence["sha256"])):
        raise ValueError("replayer evidence.sha256 must be lowercase hexadecimal")
    steps = [actions] if isinstance(actions, dict) else list(actions)
    checkpoints = receipt.get("checkpoints", [{"after_step": len(steps) - 1,
                                               "verified": True, "verified_by": verifier}])
    if isinstance(checkpoints, list):
        for checkpoint in checkpoints:
            if isinstance(checkpoint, dict):
                for flag in ("reproduced", "verified"):
                    if flag in checkpoint and checkpoint[flag] is not True:
                        raise ValueError("replayer checkpoint contradicts verified result")
    identity = {"segment": segment, "intent": intent, "steps": steps}
    digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    fields = {
        "id": "segment:" + digest,
        "params": {},
        "steps": steps,
        "checkpoints": checkpoints,
        "provenance": {"source": "wender.learn.segmenter.run_pipeline", "segment": segment},
        "evidence": evidence,
    }
    fields = _metadata_copy(fields)
    skills.admit(dict(fields, intent=intent))
    return fields


# @anchor:wa.learn.segmenter.run_pipeline
def run_pipeline(frames, engine, labeler=demonstrations.label_intent,
                 mapper=demonstrations.map_to_actions,
                 replayer=demonstrations.replay_and_verify, **segment_kwargs):
    """Run the learn-from-demonstration pipeline over in-RAM frames with injectable stages."""
    seq = list(frames)
    raw_segments = segment_frames(seq, engine, **segment_kwargs)
    outcomes = []

    for seg in raw_segments:
        seg_copy = {
            "start": seg["start"],
            "end": seg["end"],
            "peak_tiles": seg["peak_tiles"],
            "total_tiles": seg["total_tiles"],
            "bbox": dict(seg["bbox"]),
        }
        seg_frames = seq[seg["start"] : seg["end"] + 1]
        intent = None
        actions = None
        reproduced = False
        verified = False
        error = None
        handoff = {}

        try:
            intent = _metadata_copy(_call_labeler(labeler, copy.deepcopy(seg_copy), seg_frames))
        except Exception:
            error = "labeler"

        if error is None:
            try:
                actions = _metadata_copy(_validate_actions(mapper(copy.deepcopy(intent))))
            except Exception:
                actions = None
                error = "mapper"

        if error is None:
            try:
                rep = _metadata_copy(replayer(copy.deepcopy(actions), copy.deepcopy(seg_copy)))
                if (not isinstance(rep, dict)
                        or not isinstance(rep.get("reproduced"), bool)
                        or not isinstance(rep.get("verified"), bool)):
                    raise ValueError("replayer must return a dict with reproduced and verified booleans")
                reproduced = rep["reproduced"]
                verified = bool(rep["reproduced"] and rep["verified"])
                if verified:
                    handoff = _verified_handoff(intent, actions, seg_copy, rep, replayer)
            except Exception:
                reproduced = False
                verified = False
                error = "replayer"

        outcome = {
            "start": seg["start"],
            "end": seg["end"],
            "peak_tiles": seg["peak_tiles"],
            "total_tiles": seg["total_tiles"],
            "bbox": dict(seg["bbox"]),
            "intent": intent,
            "actions": actions,
            "reproduced": reproduced,
            "verified": verified,
            "error": error,
        }
        outcome.update(handoff)
        outcomes.append(outcome)

    return {
        "segments": outcomes,
        "reproduced_share_ppm": demonstrations.reproduced_share_ppm(outcomes),
    }
