# SPDX-License-Identifier: AGPL-3.0-only
"""Build and verify an offline request for an independent studio LOOK reviewer."""
from PIL import Image

VIEWS = ("front", "flank", "back")
REQUEST_SCHEMA = "viewpoint.studio-look.vision-request/v1"
RESPONSE_SCHEMA = "viewpoint.studio-look.vision-response/v1"

RUBRIC = (
    "Inspect the contact sheet itself and compare the original and candidate at matched framing "
    "and lighting. Record an observation for each of front, flank and back. Use neutral views "
    "for silhouette and broad color placement, and grazing and environment views for normals, "
    "roughness, narrow painted lines and seams. Inspect the antenna tip and attachment, hands, "
    "feet, shoulders, torso, emblem and material boundaries where present. Grade each finding "
    "MAJOR or MINOR. MAJOR means a changed silhouette or shape, missing or unrecognizable feature, "
    "wrong or misplaced color region, hole, or defect visible at production framing. MINOR means "
    "isolated texture flecks, short streaks or highlight breakups visible only in enlarged "
    "side-by-side comparisons. Keep MINOR findings in the polish backlog. REJECT requires a MAJOR "
    "finding. ACCEPT requires no MAJOR findings. Use AUSENTE with pixel_access=false and a reason "
    "if you cannot inspect the image. Return RGB samples in the requested order, with the origin "
    "at the upper-left. Pixel samples and observations must come from the sheet."
)


def samples(path):
    with Image.open(path, formats=["PNG"]) as sheet:
        width, height = sheet.size
        points = [[width * x // 100, height * y // 100] for x, y in ((12, 23), (48, 51), (83, 77))]
        rgb = sheet.convert("RGB")
        return points, [list(rgb.getpixel(tuple(point))) for point in points]


def make_request(run, sheet, hashes):
    if not isinstance(run, str) or not run.strip():
        raise ValueError("studio_vision_run_id_required")
    coordinates, _ = samples(sheet)
    return {
        "schema": REQUEST_SCHEMA,
        "response_schema": RESPONSE_SCHEMA,
        "run_id": run,
        "contact_sheet": {"path": sheet.name, "sha256": hashes["contact_sheet"]},
        "input_sha256": {key: hashes[key] for key in ("source", "selected", "metrics")},
        "pixel_coordinates": coordinates,
        "observation_views": list(VIEWS),
        "rubric": RUBRIC,
    }


def verify_response(request, request_sha, sheet, response):
    bindings = {"schema": RESPONSE_SCHEMA, "run_id": request["run_id"], "request_sha256": request_sha,
                "contact_sheet_sha256": request["contact_sheet"]["sha256"]}
    if any(response.get(key) != value for key, value in bindings.items()):
        raise ValueError("studio_vision_request_binding")
    decision = response.get("decision")
    if decision == "AUSENTE":
        reason = response.get("reason")
        if response.get("pixel_access") is not False or not isinstance(reason, str) or not reason.strip():
            raise ValueError("studio_vision_absence_unproven")
        return {"vision": decision, "absence_reason": reason.strip(), "polish_backlog": []}

    points, expected = samples(sheet)
    rgb = response.get("pixel_rgb")
    color_types_valid = (isinstance(rgb, list) and all(isinstance(row, list)
                         and all(type(channel) is int for channel in row) for row in rgb))
    observations = response.get("observations")
    if (decision not in ("ACCEPT", "REJECT") or response.get("pixel_access") is not True
            or points != request["pixel_coordinates"] or not color_types_valid or rgb != expected
            or not isinstance(observations, dict) or observations.keys() != set(VIEWS)
            or any(not isinstance(note, str) or not note.strip() for note in observations.values())):
        raise ValueError("studio_vision_pixel_access_unproven")

    findings = response.get("findings")
    if not isinstance(findings, list) or any(
        not isinstance(finding, dict) or finding.keys() != {"severity", "view", "detail"}
        or finding["view"] not in VIEWS or finding["severity"] not in ("MAJOR", "MINOR")
        or not isinstance(finding["detail"], str) or not finding["detail"].strip() for finding in findings
    ):
        raise ValueError("studio_vision_findings_invalid")
    has_major = any(item["severity"] == "MAJOR" for item in findings)
    if has_major != (decision == "REJECT"):
        raise ValueError("studio_vision_severity_decision_mismatch")
    return {"vision": decision, "observations": observations, "findings": findings,
            "polish_backlog": [item for item in findings if item["severity"] == "MINOR"]}
