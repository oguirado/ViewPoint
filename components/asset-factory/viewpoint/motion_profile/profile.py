from pathlib import Path
from hashlib import sha256
import sys

from viewpoint.host_faces.compiler import closed, digest, parse, pinned


HERE = Path(__file__).resolve().parent
OBJECT = {"type": "object"}
ARRAY = {"type": "array"}
TEXT = {"type": "string"}
HASH = {"type": "string", "pattern": "^[a-f0-9]{64}$(?!\\s)"}
NATURAL = {"type": "integer", "minimum": 1}
MODE = {"enum": ["video", "text", "supplied_bvh_with_video_clock"]}
MATCH = {"match": {"const": True}, "reference_sha256": HASH, "frames": NATURAL, "joints": NATURAL}
RESULTS = {
    "qualify": MATCH,
    "benchmark": MATCH | {"device": {"const": "cpu"}, "wall_ns": NATURAL, "peak_rss_bytes": NATURAL},
    "inspect": {"measurements": OBJECT, "joint_order": ARRAY},
    "repair": {"derived_binding": OBJECT, "derived_bvh": TEXT,
               "before_onsets_frame": OBJECT, "after_onsets_frame": OBJECT},
    "preview": {"contact_strip": ARRAY},
    "compare": MATCH,
}


def configuration(work, pins, context):
    manifest = HERE / "manifest.json"
    raw = manifest.read_bytes()
    document = parse(raw)
    bindings = {}
    for action, output in RESULTS.items():
        params = {"source_mode": MODE, "model": TEXT}
        required = ["source_mode"]
        if action == "repair":
            params |= {"destination": {"enum": ["derived", "in_place"]}, "base_revision_sha256": HASH,
                       "advance_foot_frames": {"type": "integer", "minimum": -9999, "maximum": 9999},
                       "swap_adjacent_frames": {"type": "array", "minItems": 2, "maxItems": 2,
                                                "items": {"type": "integer", "minimum": 0, "maximum": 255}}}
            required += ["destination", "base_revision_sha256"]
        result = output | {"source_mode": MODE, "scope": {"const": "offline_fixture"},
                           "rig_sha256": HASH, "bvh_sha256": HASH}
        bindings[f"ext.motion.{action}"] = {
            "mode": "offline_fixture", "manifest_sha256": digest(document),
            "exchange": "exchange", "command": [sys.executable, "-B", str(HERE / "capsule.py")],
            "timeout_s": 5, "max_output_bytes": 1048576,
            "params": closed(params, required), "result": closed(result, sorted(result)),
        }
    for name in ("base", "domain", "request", "reply"):
        pinned(pins[name])
    return {"context": context, "state": str(work / "state"), "granted_root": str(work / "granted"),
            "pins": {name: pins[name] for name in ("base", "domain", "request", "reply")},
            "manifests": [{"path": str(manifest), "sha256": sha256(raw).hexdigest()}],
            "bindings": bindings}
