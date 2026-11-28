# SPDX-License-Identifier: AGPL-3.0-only
"""Provision the original Signal Cart fixture before the factory accepts a request."""
from pathlib import Path

from viewpoint.episode_local import common as C
from viewpoint.episode_local import host

BLENDER = ("/usr/bin/blender", "-b", "--factory-startup", "--disable-autoexec",
           "-noaudio", "--python-exit-code", "2", "--python")
CREATE = Path(__file__).resolve().with_name("create_scene.py")


def make(assets, setup):
    receipt = host.scoped(setup, [CREATE.parent], [{"name": "bootstrap", "timeout": 120,
        "argv": [*BLENDER, str(CREATE), "--", str(setup / "stage.blend")]}])
    if receipt["commands"][-1]["rc"] != 0:
        raise RuntimeError(f"fixture scene failed: {setup / 'bootstrap.log'}")
    (assets / "stage.blend").write_bytes((setup / "stage.blend").read_bytes())
    for name, position, target, lens in (("Wide", [4, -7, 4], [0, 0, 0.30], 42),
                                        ("Signal", [3, -5, 2.5], [0.62, 0.16, 0.42], 52)):
        C.write(assets / f"{name}.json", {"name": name, "location_m": position, "target_m": target,
                                       "lens_mm": lens, "sensor_width_mm": 36})
    shots = []
    for shot_id, camera, xs, signal in (("arrival", "Wide", [-1.6, -0.8, 0.1], [False] * 3),
                                        ("activation", "Signal", [0.1, 0.65, 0.8], [False, True, True])):
        clip_name = shot_id + "-clip.json"
        C.write(assets / clip_name, {"name": shot_id + "-action", "actor": "Cart", "signal": "Beacon",
            "keys": [{"frame": frame, "location": [x, 0, 0.34], "signal_on": on}
                     for frame, x, on in zip((1, 3, 5), xs, signal, strict=True)]})
        def ref(filename):
            return {"path": filename, "sha256": C.sha_file(assets / filename)}
        shots.append({"id": shot_id, "scene": ref("stage.blend"), "clip": ref(clip_name),
                      "camera": ref(f"{camera}.json"), "source": [1, 5], "cut": [2, 5]})
    recipe = {"schema": "viewpoint.episode-recipe/v1", "episode_id": "signal-cart", "fps": 24,
              "pixels": [320, 180], "shots": shots,
              "cues": [{"frame": 5, "duration_frames": 3, "hz": 560, "caption": "[Signal chirps]"}]}
    policy = {"schema": "viewpoint.episode-policy/v1", "profile": "local-private-archive",
              "rights": {"source": "Original Signal Cart fixture", "license": "CC0-1.0",
                         "effects": ["render", "mux", "archive", "restore"]},
              "bindings": {shot["id"]: {key: shot[key] for key in ("scene", "clip", "camera")} for shot in shots},
              "ceiling": {"shots": 2, "render_frames": 10, "pixels_per_frame": 57600},
              "destination": "private-local"}
    C.write(assets / "recipe.json", recipe)
    return policy
