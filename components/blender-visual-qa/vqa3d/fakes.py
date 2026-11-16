"""Recorded live evidence, strict replay, and explicit provenance.

Replay proves regression behaviour only. It does not qualify the current runtime,
renderer accuracy or cross-platform determinism. Fault tests modify copied records
and reseal them; those synthetic variants are never represented as live evidence.
"""
import base64
import json
import os
from pathlib import Path
import tempfile

from .canon import canonical_json, sha256_canonical
from .contract import EnvAdapter, Observation
from .verdict import PASS, FAIL, rc_of

TRACE_SCHEMA = "vqa3d.trace/v1"


def compare_replay_runs(first, second):
    """Compare receipts from independent runs of the SAME model and scenario.

    Complete validated observations are required; matching missing evidence is
    not determinism. This qualifies these runs only, not cross-host behaviour.
    """
    verdict, reason = PASS, "replay_receipts_identical"
    if not first or not second or len(first) != len(second):
        verdict, reason = FAIL, "replay_run_length_mismatch_or_empty"
    else:
        for index, (left, right) in enumerate(zip(first, second)):
            for receipt in (left, right):
                payload = dict(receipt)
                digest = payload.pop("receipt_sha256", None)
                complete = receipt.get("state_comparison_available") is True and all(
                    receipt.get(tag, {}).get(key) is not None
                    for tag in ("before", "after") for key in ("obs_id", "state_sha256"))
                if digest != sha256_canonical(payload) or not complete:
                    return {"final_verdict": FAIL, "verdict_rc": rc_of(FAIL), "reason": "replay_receipt_invalid_or_incomplete"}
            if canonical_json(left) != canonical_json(right):
                verdict, reason = FAIL, "nondeterministic_replay:step:" + str(index)
                break
    return {"final_verdict": verdict, "verdict_rc": rc_of(verdict), "reason": reason}


def observation_record(obs, tag):
    return {"tag": tag, "obs_id": obs.obs_id, "frame_sha256": obs.frame_sha256,
            "state_sha256": obs.state_sha256, "width": obs.width, "height": obs.height,
            "capture_available": obs.capture_available, "state": obs.state,
            "notes": list(obs.notes), "camera_source": obs.camera_source, "camera_name": obs.camera_name, "frame_png_base64":
            base64.b64encode(obs.frame_png).decode("ascii") if obs.frame_png is not None else None}


def observation_from_record(row):
    return Observation(row["obs_id"], base64.b64decode(row["frame_png_base64"], validate=True)
                       if row["frame_png_base64"] is not None else None,
                       row["frame_sha256"], row["width"], row["height"], row["state"],
                       row["state_sha256"], row["capture_available"], tuple(row["notes"]),
                       row.get("camera_source", "none"), row.get("camera_name", ""))


def seal_trace(trace):
    trace = json.loads(canonical_json(trace))
    trace.pop("trace_sha256", None)
    trace["trace_sha256"] = sha256_canonical(trace)
    return trace


def atomic_json(path, value):
    """Publish only fully serialized data, preserving existing bytes on refusal."""
    payload = (canonical_json(value) + "\n").encode("utf-8")
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class RecordingAdapter(EnvAdapter):
    def __init__(self, adapter):
        self.wrapped = adapter
        self.id, self.version, self.status = adapter.id, adapter.version, adapter.status
        self.events = []

    @property
    def capabilities(self):
        return self.wrapped.capabilities

    def probe(self, render=False):
        result = self.wrapped.probe(render=True) if render else self.wrapped.probe()
        self.events.append({"kind": "probe", "render": render, "result": result})
        return result

    def sample(self, frames, paths):
        from .temporal import sample_adapter
        result = sample_adapter(self.wrapped, frames, paths)
        result = {str(k): v for k, v in result.items()}
        self.events.append({"kind": "sample", "frames": frames, "paths": paths, "result": result})
        return result

    def capture(self, tag):
        obs = self.wrapped.capture(tag)
        obs.validate(self.id, tag)
        if obs.width > 96 or obs.height > 72:
            raise ValueError("recorded_frame_too_large")
        self.events.append({"kind": "capture", "observation": observation_record(obs, tag)})
        return obs

    def scene_state(self):
        return self.wrapped.scene_state()

    def act(self, action):
        canonical_json(action)  # Refuse unrecordable input before applying it.
        result = self.wrapped.act(action)
        self.events.append({"kind": "act", "action": action, "result": result})
        return result

    def trace(self, scenario, receipts, provenance):
        return seal_trace({"schema": TRACE_SCHEMA,
                           "adapter": {"id": self.id, "version": self.version, "status": self.status},
                           "provenance": provenance, "scenario": scenario,
                           "events": self.events, "receipts": receipts})

    def close(self):
        return self.wrapped.close()


class ReplayAdapter(EnvAdapter):
    def __init__(self, trace):
        if isinstance(trace, (str, Path)):
            trace = json.loads(Path(trace).read_text(encoding="utf-8"))
        self.trace = json.loads(canonical_json(trace))
        payload = dict(self.trace)
        digest = payload.pop("trace_sha256", None)
        if self.trace.get("schema") != TRACE_SCHEMA or sha256_canonical(payload) != digest:
            raise ValueError("trace_integrity_mismatch")
        meta = self.trace["adapter"]
        self.id, self.version, self.status = meta["id"], meta["version"], meta["status"]
        self._cursor = 0
        self._closed = False
        self._state = None
        self.capabilities = {"capture_available": all(e["observation"]["capture_available"]
                             for e in self.trace["events"] if e["kind"] == "capture"),
                             "scene_state": True, "act": True}

    def _next(self, kind):
        if self._closed:
            raise RuntimeError("replay_closed")
        if self._cursor >= len(self.trace["events"]):
            raise ValueError("replay_exhausted")
        event = self.trace["events"][self._cursor]
        if event["kind"] != kind:
            raise ValueError("replay_event_mismatch")
        self._cursor += 1
        return event

    def sample(self, frames, paths):
        event = self._next("sample")
        if frames != event["frames"] or paths != event["paths"]:
            raise ValueError("replay_sample_mismatch")
        return event["result"]

    def probe(self, render=False):
        if render or (self._cursor < len(self.trace["events"]) and self.trace["events"][self._cursor]["kind"] == "probe"):
            event = self._next("probe")
            if render != event["render"]:
                raise ValueError("replay_probe_mismatch")
            return event["result"]
        return {"id": self.id, "version": self.version, "status": self.status,
                "recorded": True, "available": True, "capabilities": dict(self.capabilities),
                "provenance": self.trace["provenance"]}

    def capture(self, tag):
        row = self._next("capture")["observation"]
        if row["tag"] != tag:
            raise ValueError("replay_tag_mismatch")
        obs = observation_from_record(row)
        self._state = obs.state
        return obs

    def scene_state(self):
        if self._state is None:
            raise RuntimeError("replay_not_observed")
        return json.loads(canonical_json(self._state))

    def act(self, action):
        event = self._next("act")
        if canonical_json(action) != canonical_json(event["action"]):
            raise ValueError("replay_action_mismatch")
        return event["result"]

    def close(self):
        self._closed = True
