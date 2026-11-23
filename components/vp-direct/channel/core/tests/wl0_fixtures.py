"""Use the same receipt bytes at every host qualification boundary."""

import json
from pathlib import Path


def receipt_cases():
    original = (Path(__file__).resolve().parents[3] / "config/wl0.json").read_bytes()
    yield "reviewed receipt", original, True

    def changed(section, field, value):
        root = json.loads(original)
        row = root
        for key in section:
            row = row[key]
        row[field] = value
        return json.dumps(root, sort_keys=True).encode()

    yield "unrelated addition", changed(("steps", "L0-4"), "diagnostic_note", "sample retained"), True
    yield "reformatted receipt", original + b"\n", True
    yield "wrong schema", changed((), "schema", "wrong"), False
    yield "wrong source digest", changed(("source",), "sha256", "0" * 64), False
    yield "changed capture facts", changed(("steps", "L0-4"), "frames_differ", False), False
    yield "changed action scope", changed(("steps", "L0-8"), "action_index", 1), False
    yield "changed app facts", changed(("apps", "google-chrome", "atspi"), "available", False), False
    yield "malformed app facts", changed(("apps",), "google-chrome", []), False
    yield "truncated JSON", original[:-25], False
    yield "missing file", None, False
