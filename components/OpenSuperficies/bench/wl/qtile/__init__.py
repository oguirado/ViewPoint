# Pure helpers for the qtile WM profile (data only; no Qtile import, no build).
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_LAYOUTS = {(1920, 1080): "layout_1080p.json", (3840, 2160): "layout_4k.json"}
PROFILES = ("default", "qtile")


def load_layout(width, height):
    name = _LAYOUTS.get((width, height))
    if name is None:
        raise ValueError("E_SCHEMA: no qtile layout for %dx%d" % (width, height))
    with open(os.path.join(_HERE, name)) as f:
        return json.load(f)


ATTESTATION_SCHEMA = "os.cu.qtile-image-attestation/v1"
_FIELDS = ("schema", "image", "digest", "qtile_version", "approval_id")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_BYTES = 4096


def attestation_ok(path):
    """True iff path is a JSON object with exactly the closed attestation schema."""
    try:
        if not path or not os.path.isfile(path) or os.path.getsize(path) > _MAX_BYTES:
            return False
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(d, dict) or set(d) != set(_FIELDS):
        return False
    if not all(isinstance(d[k], str) and d[k].strip() for k in _FIELDS):
        return False
    return d["schema"] == ATTESTATION_SCHEMA and _DIGEST.match(d["digest"]) is not None


def profile_gate(wm_profile, attestation_path):
    """(rc, code): qtile needs the operator-approved image attestation (closed-schema JSON; no build here)."""
    if wm_profile not in PROFILES:
        return 2, "E_SCHEMA"
    if wm_profile == "qtile" and not attestation_ok(attestation_path):
        return 5, "image_layer_missing"
    return 0, "ok"
