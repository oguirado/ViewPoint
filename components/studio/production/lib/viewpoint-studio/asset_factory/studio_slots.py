# SPDX-License-Identifier: AGPL-3.0-only
"""Studio operations added to the existing Asset Factory slot contracts."""

EXTENSIONS = {
    "layout": (
        "vp.studio.layout/v1", ("scene_blend", "shot_intent"),
        ("layout_scene", "camera_binding", "light_state"),
    ),
    "episode": (
        "vp.studio.episode/v1", ("episode_recipe", "resolved_assets"),
        ("bound_episode",),
    ),
    "conform": (
        "vp.studio.conform/v1", ("bound_episode", "rendered_shots"),
        ("editable_timeline", "conformed_picture", "editorial_receipt"),
    ),
    "sound": (
        "vp.studio.sound/v1", ("picture_lock", "stems", "cue_sheet"),
        ("editable_mix", "mixed_audio"),
    ),
    "captions": (
        "vp.studio.captions/v1", ("picture_lock", "cue_sheet"),
        ("captions_srt",),
    ),
    "library": (
        "vp.studio.library/v1", ("text", "catalog", "destination_policy"),
        ("eligible_assets",),
    ),
}
