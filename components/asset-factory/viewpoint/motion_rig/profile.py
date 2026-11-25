# SPDX-License-Identifier: AGPL-3.0-only
"""Bind the offline motion and rig mini-tools to the same HOST-FACES configuration."""

from hashlib import sha256

from viewpoint.host_faces.compiler import parse
from viewpoint.motion_profile.profile import configuration
from viewpoint.rig_profile.configure import HERE as RIG_DIR, bindings


def configure(work, pins, context, *, rig_reference_sha256=()):
    config = configuration(work, pins, context)
    manifest = RIG_DIR / "rig.slot.json"
    raw = manifest.read_bytes()
    config["manifests"].append({"path": str(manifest), "sha256": sha256(raw).hexdigest()})
    config["bindings"].update(bindings(parse(raw), rig_reference_sha256))
    return config
