# SPDX-License-Identifier: AGPL-3.0-only
"""Group raster registrations into shots and stitch witnessed tiles."""
from dataclasses import replace
from itertools import chain, groupby
from math import ceil

from .calibration import calibrate
from .frames import read_frames
from .raster import HEIGHT, SKY_PERIOD, TILE, WIDTH, floor_uncertainty, hud_descriptor, measure
from .validation import validate_scene


MAX_PAN_PX_PER_SECOND = 100  # Beacon Run's authored maximum movement in raster pixels.


def _pan_budget(frame, other):
    elapsed = abs(frame.ticks - other.ticks) * frame.time_base
    # Round-to-nearest camera positions can widen the displacement by one pixel.
    return 1 + MAX_PAN_PX_PER_SECOND * elapsed


def _nearest_wrap(phase, prior_x, period):
    return max(0, (prior_x - phase + period // 2) // period)


def _unwrap(raster, prior_x, budget, wrap_uncertainty, period, size):
    phase, y = raster.offset
    local_error, y_error = raster.uncertainty
    # Choose the nearest nonnegative alias, including through backward pans.
    wraps = _nearest_wrap(phase, prior_x, period)
    x = phase + wraps * period
    # A long gap can admit additional periods. That ambiguity persists until
    # a visible origin anchor, even when subsequent motion is well sampled.
    forward = int((budget + prior_x - x) // period)
    backward = min(wraps, int((budget + x - prior_x) // period))
    alternative_periods = max(0, forward, backward)
    wrap_uncertainty += alternative_periods * period
    cells = {}
    partial = {}
    columns, remainder = divmod(wraps * period, size)
    if not (wrap_uncertainty or remainder):
        cells = {(r, c + columns): code for (r, c), code in raster.cells.items()}
        partial = {(r, c + columns): box for (r, c), box in raster.partial_solids.items()}
    return replace(raster, offset=(x, y), uncertainty=(local_error + wrap_uncertainty, y_error),
                   cells=cells, partial_solids=partial), wrap_uncertainty


def _measurements(frames, model):
    prior_y = 0
    measure_frame = model.measure if model else measure
    for frame in frames:
        raster = measure_frame(frame.image, prior_y)
        if raster.offset is not None:
            prior_y = raster.offset[1]
        yield frame, raster


def _camera_step(raster, prior, budget, wrap_uncertainty, hud_boundary, period, size):
    phase, y = raster.offset
    prior_x, prior_y = prior.offset
    local_x_error = prior.uncertainty[0] - wrap_uncertainty
    x_budget = budget + local_x_error + raster.uncertainty[0]
    y_budget = budget + prior.uncertainty[1] + raster.uncertainty[1]
    prior_phase = prior_x % period
    delta = abs(phase - prior_phase)
    circular_step = min(delta, period - delta)
    wraps = _nearest_wrap(phase, prior_x, period)
    # With a known period, a putative step through negative X is impossible.
    outside_level = not wrap_uncertainty and abs(phase + wraps * period - prior_x) > x_budget
    camera_cut = circular_step > x_budget or outside_level or abs(y - prior_y) > y_budget

    if raster.at_spawn:
        # A locally displaced phase can still witness a respawn after an ambiguous gap.
        away_from_origin = prior_x > prior.uncertainty[0] or prior_phase > local_x_error
        return raster, 0, camera_cut or away_from_origin

    # A HUD boundary supplies an independent cut cue when continuing would
    # certify a carried period. First-period HUD changes can keep a local pan.
    if camera_cut or (hud_boundary and (wraps or wrap_uncertainty)):
        ambiguity = max(period, prior_x - prior_phase + wrap_uncertainty)
        return replace(raster, uncertainty=(raster.uncertainty[0] + ambiguity, raster.uncertainty[1]),
                       cells={}, partial_solids={}), ambiguity, camera_cut
    projected, ambiguity = _unwrap(raster, prior_x, x_budget, wrap_uncertainty, period, size)
    return projected, ambiguity, False


def _partition(frames, model=None):
    measured = _measurements(frames, model)
    period, size = SKY_PERIOD, TILE
    if model is not None:
        period = model.period
        size = model.terrain.size if model.terrain else 1
    leading = []
    for frame, raster in measured:
        if raster.offset is not None:
            first_anchor = frame, raster
            break
        leading.append((frame, raster))
    else:
        if model is None:
            raise ValueError("unresolved synthetic sky registration for the entire recording")
        first_anchor = None

    shot = -1
    previous_panel = None
    latest_anchor = None
    wrap_uncertainty = 0
    pending_hud_boundary = False
    anchors = () if first_anchor is None else (first_anchor,)
    for frame, raster in chain(leading, anchors, measured):
        hud_boundary = shot >= 0 and previous_panel != raster.panel
        pending_hud_boundary |= hud_boundary
        reason = "hud_transition" if hud_boundary else None
        if shot == -1:
            reason = "recording_start"
        if raster.offset is None:
            hint = latest_anchor or first_anchor
            if hint is None:
                raster = replace(raster, offset=(0, 0), uncertainty=(period, model.height))
            else:
                anchor_frame, anchor = hint
                x_error = anchor.uncertainty[0] + ceil(_pan_budget(frame, anchor_frame))
                y_error = (max(size, anchor.uncertainty[1]) if model
                           else floor_uncertainty(anchor.offset[1]))
                raster = replace(raster, offset=anchor.offset, uncertainty=(x_error, y_error))
        else:
            if latest_anchor is not None:
                prior_frame, prior = latest_anchor
                raster, wrap_uncertainty, cut = _camera_step(
                    raster, prior, _pan_budget(frame, prior_frame), wrap_uncertainty, pending_hud_boundary, period, size)
                if model is not None and cut and raster.at_spawn:
                    # A witnessed origin reset changes the camera within the
                    # current game screen. A simultaneous HUD cut still stands.
                    raster = replace(raster, camera_reset=True)
                if cut and reason is None and not raster.camera_reset:
                    reason = "camera_discontinuity"
                if model is not None and wrap_uncertainty:
                    raster = replace(raster, degraded=(*raster.degraded, "camera_period_unresolved"))
                if model is not None and (raster.offset[0] // period * period) % size:
                    raster = replace(raster, degraded=(*raster.degraded, "tile_registration_unresolved"))
            latest_anchor = frame, raster
            pending_hud_boundary = False
        if reason:
            shot += 1
        yield shot, (frame, raster, reason)
        previous_panel = raster.panel


def _stitch(shot_id, records, viewport=(WIDTH, HEIGHT), size=TILE):
    observations, samples, evidence, cell_candidates = [], [], [], {}
    width, height = viewport
    width_px, height_px = viewport
    for frame, raster, reason in records:
        if not observations:
            start_reason = reason
        provenance = frame.provenance
        evidence.append(provenance)
        samples.append({"offset_px": list(raster.offset), "provenance": dict(provenance, label="inferred")})
        observations.append({"frame_index": frame.index, "pts_ticks": frame.ticks,
                             "rgb_sha256": frame.rgb_sha256, "uncertainty_px": list(raster.uncertainty),
                             "hud": raster.hud if raster.hud is not None else hud_descriptor(raster.panel)})
        if raster.degraded:
            observations[-1]["degraded"] = list(raster.degraded)
        if raster.camera_reset:
            observations[-1]["camera_reset"] = True
        if raster.cells or raster.partial_solids:
            width_px = max(width_px, width + raster.offset[0])
            height_px = max(height_px, height + raster.offset[1])
        for cell, code in raster.cells.items():
            candidates = cell_candidates.setdefault(cell, {})
            if code not in candidates or candidates[code][1] is not None:
                candidates[code] = frame.index, None
        for cell, box in raster.partial_solids.items():
            cell_candidates.setdefault(cell, {}).setdefault("#", (frame.index, box))
    grid = ([[None]] if size is None else
            [[None] * ((width_px + size - 1) // size) for _ in range((height_px + size - 1) // size)])
    witnesses = []
    for (row, col), candidates in sorted(cell_candidates.items()):
        if len(candidates) == 1:
            code, (frame_index, box) = next(iter(candidates.items()))
            witness = {"row": row, "column": col, "frame_index": frame_index}
            if box is None:
                grid[row][col] = code
            else:
                witness["visible_xywh"] = list(box)
            witnesses.append(witness)
    return {"shot_id": shot_id, "geometry_epoch": shot_id, "start_reason": start_reason,
            "observations": observations, "tile_witnesses": witnesses,
            "level_map": {"schema": "rea.game.level_map/v1", "level_id": f"shot-{shot_id}",
                          "camera_samples": samples, "regions": [], "evidence": evidence,
                          "derived_from": list(dict.fromkeys(item["frame_sha256"] for item in evidence)),
                          "tiles": {"tile_size_px": size if size is not None else 1, "origin_px": [0, 0], "grid": grid,
                                    "legend": {".": "air", "#": "solid"}}}}


def reconstruct(directory, *, project_partial_solids=False):
    """Return a validated scene from one W1 directory.

    Grid-based raster consumers can project witnessed solid fragments. The
    clipped witness still describes the extent of the direct observation.
    """
    frames = list(read_frames(directory))
    first = frames[0]
    base = first.time_base
    model = calibrate(frames)
    viewport = model.width, model.height
    size = model.terrain.size if model.terrain else None
    shots = [_stitch(shot_id, (entry[1] for entry in group), viewport, size)
             for shot_id, group in groupby(_partition(frames, model), key=lambda entry: entry[0])]
    if project_partial_solids:
        for shot in shots:
            grid = shot["level_map"]["tiles"]["grid"]
            for witness in shot["tile_witnesses"]:
                if "visible_xywh" in witness:
                    grid[witness["row"]][witness["column"]] = "#"
    return validate_scene({"video_sha256": first.video_sha256, "viewport_px": list(viewport),
                           "time_base": {"numerator": base.numerator, "denominator": base.denominator},
                           "shots": shots})
