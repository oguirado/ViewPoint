import re
import sys
from pathlib import Path

from viewpoint.host_faces.compiler import closed, digest


HERE = Path(__file__).resolve().parent
OPERATIONS = {"qualify", "benchmark", "inspect", "repair", "preview", "compare"}
SHA = {"type": "string", "pattern": r"^[0-9a-f]{64}$(?!\s)"}
STR, OBJ, ARR, INT, BOOL = ({"type": name} for name in ("string", "object", "array", "integer", "boolean"))
RESULTS = {
    "qualify": {"reference_sha256": SHA, "qualification": STR, "live_use_permitted": BOOL,
                "joints": INT, "vertices": INT, "pose_samples": INT},
    "benchmark": {"reference_sha256": SHA, "device": STR, "cold_measure_ns": INT,
                  "warm_measure_ns": ARR, "peak_rss_bytes": INT},
    "inspect": {"structure": OBJ, "issues": ARR},
    "repair": {"derived_ref": STR, "derived_sha256": SHA, "repaired_vertices": ARR, "before": OBJ, "after": OBJ},
    "preview": {"perception_level": STR, "kind": STR, "poses": ARR},
    "compare": {"reference_sha256": SHA, "equal": BOOL, "changed_fields": ARR},
}
PARAMETERS = {
    "repair": {"base_revision_sha256": SHA, "target": STR},
    "benchmark": {"device": {"enum": ["cpu", "gpu"]}, "samples": INT | {"minimum": 2, "maximum": 10}},
}


def bindings(manifest, trusted_references=()):
    references = sorted(set(trusted_references))
    if any(type(pin) is not str or re.fullmatch(r"[0-9a-f]{64}", pin) is None for pin in references):
        raise ValueError("Invalid trusted rig reference digest")
    if manifest["slot_id"] != "rig" or {operation["id"] for operation in manifest["operations"]} != OPERATIONS:
        raise ValueError("Wrong rig manifest")
    result = {}
    for operation in manifest["operations"]:
        name = operation["id"]
        required = PARAMETERS.get(name, {})
        params = required | {"model": STR}
        properties = {"asset_sha256": SHA, "profile": {"const": "rp4.rig.offline_fixture/v1"}} | RESULTS[name]
        result[f"ext.rig.{name}"] = {"mode": "offline_fixture", "manifest_sha256": digest(manifest),
                                     "exchange": "capsule", "command": [sys.executable, "-B", str(HERE / "adapter.py"), *references],
                                     "timeout_s": 5, "max_output_bytes": 262144,
                                     "params": closed(params, required), "result": closed(properties, properties)}
    return result
