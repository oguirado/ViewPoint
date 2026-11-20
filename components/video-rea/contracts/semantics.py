# SPDX-License-Identifier: AGPL-3.0-only
"""Cross-field checks for records that have already passed their JSON Schema."""


def check_record(record):
    """Raise ValueError on inconsistent physics bounds, tile grids or held-out splits."""
    schema = record["schema"]
    if schema == "rea.game.physics/v1":
        for name in ("gravity", "run_speed", "jump_velocity", "friction", "pixel_to_world", "tick_hz"):
            estimate = record.get(name)
            if estimate is None or estimate["value"] is None:
                continue
            if not estimate["lower"] <= estimate["value"] <= estimate["upper"]:
                raise ValueError(f"{name} must satisfy lower <= value <= upper")
    elif schema == "rea.game.level_map/v1":
        tiles = record["tiles"]
        width = len(tiles["grid"][0])
        for row in tiles["grid"]:
            if len(row) != width:
                raise ValueError("tile grid rows have unequal widths")
            for code in row:
                if code is not None and code not in tiles["legend"]:
                    raise ValueError(f"tile code {code!r} is missing from the legend")
    elif schema == "rea.verification/v1":
        train = set(record["train_frames_sha256"])
        if any(witness["frame_sha256"] in train for witness in record["held_out"]):
            raise ValueError("held-out frame digest occurs in the training set")
