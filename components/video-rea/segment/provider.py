# SPDX-License-Identifier: AGPL-3.0-only
"""Segment admitted recordings and publish a single artifact through the caller's sink."""
from collections.abc import Mapping
from pathlib import Path
import sys

from source.records import canonical, digest

from .geometry import area, detect, visible_boxes
from .inputs import Recording, locator
from .ocr import TESSDATA, open_ocr
from .temporal import compare, descriptor_return


REPORT = "segmentation.json"


def element(detection, witness, ocr, rgb):
    text, reason = ocr.read(rgb, detection)
    provenance = witness["provenance"]
    element_id = digest(canonical([detection.kind, detection.layer, detection.bbox]))
    bounds = detection.bbox
    if detection.visible != (bounds,):
        # Keep one observation per control. Pick the rectangle with the best
        # coverage of the complete visible shape, including occlusion as error.
        visible_area = sum(map(area, detection.visible))
        largest = max(detection.visible, key=area)
        if area(largest) * area(bounds) > visible_area**2:
            bounds = largest
    record = {
        "schema": "rea.ui_element/v1", "provenance": provenance, "status": "ok",
        "kind": detection.kind, "bbox": bounds, "text": text,
        "confidence": 0.80, "degraded_reason": None, "element_id": element_id,
    }
    return {
        "record": record, "sha256": digest(canonical(record)), "layer": detection.layer,
        "visible_regions": detection.visible, "ink_bbox": detection.ink,
        "small": bounds[2] < 44 or bounds[3] < 24,
        "ocr_reason": reason,
    }


def analyze(inputs, tessdata_dir=TESSDATA, diagnostics=None):
    recording = Recording(inputs.read)
    # W1's manifest validator loads the sibling visioncore package.
    engine = str(Path(__file__).resolve().parents[2] / "vision-engine")
    sys.path.insert(0, engine)
    try:
        from vadapt.cu_marks import candidates
    finally:
        sys.path.remove(engine)
    from visioncore.change import global_vector

    frames, boundaries = [], []
    backdrop = backdrop_rows = None
    menu_free = None
    previous_rgb = previous_detections = previous_witness = None
    older_rgb = older_rows = None
    state = 0
    observations = iter(recording)
    observation = next(observations, None)

    def owner_gap(before, after):
        lo, hi = before["pts_ticks"], after["pts_ticks"]
        return any(g["kind"] == "owner_gap" and lo <= g["pts"] < hi for g in recording.gaps)

    with open_ocr(tessdata_dir) as ocr:
        while observation is not None:
            rgb, witness = observation
            next_observation = next(observations, None)
            alternative = menu_free if menu_free is not None and menu_free[0] is not backdrop else None
            if previous_witness is not None and witness["duplicate_of"] == previous_witness["occurrence"]:
                detections = previous_detections
            else:
                detections = detect(rgb, backdrop, backdrop_rows or (), candidates(rgb, extra=True),
                                    alternative=alternative)
            evidence = None
            if previous_rgb is not None:
                returns = older_rgb is not None and descriptor_return(
                    older_rows, detections, older_rgb, rgb)
                exact_recurrence = witness["duplicate_of"]
                # A local one-frame departure that immediately returns is transient.
                # Preserve durable revisits and large screen/overlay changes.
                evidence = compare(previous_rgb, rgb, previous_detections, detections, global_vector)
                if evidence["boundary"] and evidence["changed_pixels"] * 25 < rgb.shape[0]*rgb.shape[1]:
                    returns_next = False
                    if next_observation is not None and not owner_gap(witness, next_observation[1]):
                        next_rgb, next_witness = next_observation
                        returns_next = next_witness["rgb_sha256"] == previous_witness["rgb_sha256"]
                        if not returns_next:
                            next_rows = detect(next_rgb, backdrop, backdrop_rows or (),
                                               candidates(next_rgb, extra=True), alternative=alternative)
                            returns_next = descriptor_return(
                                previous_detections, next_rows, previous_rgb, next_rgb)
                    if returns_next or (returns and frames[-1]["change"]
                                        and frames[-1]["change"].get("transient_return")):
                        evidence["boundary"] = False
                        evidence["transient_return"] = True
                evidence["descriptor_return"] = returns
                evidence["exact_prior_occurrence"] = exact_recurrence
                evidence["owner_gap"] = owner_gap(previous_witness, witness)
                if evidence["boundary"] and not evidence["owner_gap"]:
                    state += 1
                    boundaries.append({"before": previous_witness, "after": witness, "evidence": evidence})
            elements = [element(row, witness, ocr, rgb) for row in detections]
            reasons = [] if elements else ["no_visible_widgets"]
            if ocr.reason:
                reasons.append(ocr.reason)
            screen = {
                "schema": "rea.screen/v1", "provenance": witness["provenance"],
                "screen_id": f"segment-{state}", "element_sha256": [row["sha256"] for row in elements],
                "status": "degraded" if reasons else "ok", "degraded_reasons": reasons,
            }
            raster = (0, 0, recording.width, recording.height)
            dialogs = [row.bbox for row in detections if row.kind == "dialog" and row.layer == "foreground"]
            parent = (raster,)
            for cover in dialogs:
                parent = tuple(part for box in parent for part in visible_boxes(box, cover))
            if not dialogs:
                backdrop, backdrop_rows = rgb, detections
                if not any(row.kind == "menu" and row.container for row in detections):
                    menu_free = (rgb, detections)
            surfaces = {"foreground": dialogs or [raster], "parent": parent if dialogs else [],
                        "menus": [d.bbox for d in detections if d.kind == "menu" and d.container]}
            frames.append({"source": witness, "screen": screen, "elements": elements,
                           "surfaces": surfaces, "change": evidence})
            older_rgb, older_rows = previous_rgb, previous_detections
            previous_rgb, previous_detections, previous_witness = rgb, detections, witness
            observation = next_observation
        if diagnostics is not None:
            diagnostics.update(cold_ns=ocr.cold_ns, warm_calls=ocr.calls, warm_total_ns=ocr.total_ns,
                               warm_ns=ocr.warm_ns, cache_hits=ocr.hits)
        return {"schema": "rea.segmentation/v1", "derived_from": recording.parents, "video_sha256": recording.source,
                "reference_size": [recording.width, recording.height], "time_base": recording.time_base,
                "ocr": {"engine": ocr.engine, "reason": ocr.reason,
                        "reads": ocr.calls, "cache_hits": ocr.hits, "timeouts": ocr.timeouts}, "frames": frames,
                "boundaries": boundaries, "clock_gaps": recording.gaps}


def segment(args, inputs, sink):
    """Read admitted inputs with inputs.read(name); publish with sink.write(name, bytes).

    The transport owns admission, revocation and the sink transaction. The sink
    returns the sink-relative path committed for this content.
    """
    accepted = {"op", "goal_id", "inputs", "seed", "permit_ref", "dry_run", "tessdata_dir"}
    if not isinstance(args, Mapping) or args.keys() - accepted:
        raise ValueError("unexpected segmentation options")
    tessdata = args.get("tessdata_dir", TESSDATA)
    if not isinstance(tessdata, (str, Path)) or not str(tessdata):
        raise ValueError("tessdata_dir must be a nonempty directory path")
    if args.get("op") != "rea.segment":
        raise ValueError("expected rea.segment selector")
    if args.get("dry_run", False):
        raise ValueError("dry-run is handled by the transport")
    data = canonical(analyze(inputs, tessdata))
    sha = digest(data)
    path = sink.write(REPORT, data)
    return {
        "artifact_refs": [{
            "schema": "vp.artifact_ref/v1", "locator": locator(sha), "sha256": sha,
            "bytes": len(data), "media_type": "application/json", "stage": "record", "copy": "none",
        }],
        "receipt": {"path": path, "sha256": sha},
    }
