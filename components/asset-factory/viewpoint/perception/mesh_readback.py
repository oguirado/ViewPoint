# SPDX-License-Identifier: AGPL-3.0-only
"""Independent reader for the offline structured mesh prediction fixture."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import sys
from time import monotonic_ns


IDENTITY = ("request_id", "scene_sha256", "camera_sha256", "rig_sha256",
            "frame_id", "exporter_id")
LIMIT = 1024 * 1024


class Refused(ValueError):
    """A declared fixture contract rejection."""


def require(condition, code):
    if not condition:
        raise Refused(code)


def pin(data):
    return sha256(data).hexdigest()


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def invalid_constant(value):
    raise Refused("INVALID_JSON")


def parse(raw):
    require(len(raw) <= LIMIT, "FIXTURE_TOO_LARGE")
    try:
        return json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise Refused("INVALID_JSON") from exc
    except ValueError as exc:
        if isinstance(exc, Refused):
            raise
        raise Refused("INVALID_JSON") from exc


def read_bytes(directory, name):
    try:
        fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "FIXTURE_FILE_INVALID")
            raw = stream.read(LIMIT + 1)
    except OSError as exc:
        raise Refused("FIXTURE_FILE_INVALID") from exc
    require(len(raw) <= LIMIT, "FIXTURE_TOO_LARGE")
    return raw


def read(directory, name):
    raw = read_bytes(directory, name)
    return parse(raw), pin(raw)


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()



def shape(row, fields, code):
    require(type(row) is dict and row.keys() == set(fields), code)


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)


def validate_identity(identity):
    shape(identity, IDENTITY, "IDENTITY_SCHEMA")
    require(all(digest(identity[key]) if key.endswith("_sha256") else identifier(identity[key])
                for key in IDENTITY), "IDENTITY_SCHEMA")


def differences(before, after):
    return [key for key in IDENTITY if before[key] != after[key]]


def request_and_receipt(directory):
    request, request_pin = read(directory, "request.json")
    receipt, receipt_pin = read(directory, "producer-receipt.json")
    shape(request, ("identity", "marker"), "REQUEST_SCHEMA")
    shape(receipt, ("identity", "request_sha256", "artifact_sha256"), "RECEIPT_SCHEMA")
    validate_identity(request["identity"])
    validate_identity(receipt["identity"])
    require(identifier(request["marker"]) and digest(receipt["artifact_sha256"]), "REQUEST_OR_ARTIFACT_SCHEMA")
    require(receipt["identity"] == request["identity"] and receipt["request_sha256"] == request_pin,
            "RECEIPT_REQUEST_MISMATCH")
    return request, receipt, receipt_pin


def patch_hits(objects, patch_id, marker):
    require(type(objects) is dict, "PATCH_SCHEMA")
    hits = []
    for object_id, cue in objects.items():
        require(identifier(object_id) and identifier(cue), "OBJECT_SCHEMA")
        if cue == marker:
            hits.append({"patch_id": patch_id, "object_id": object_id})
    return hits


def patch_filename(patch_id):
    require(identifier(patch_id), "PATCH_SCHEMA")
    return f"patch-{patch_id}.json"


def validate_hint(hint):
    shape(hint, ("predict", "ranked_patch", "crop"), "HINT_SCHEMA")
    use_prediction, ranked_patch, crop = (hint[key] for key in ("predict", "ranked_patch", "crop"))
    require(type(use_prediction) is bool and (ranked_patch is None or identifier(ranked_patch))
            and (crop is None or type(crop) is dict), "HINT_SCHEMA")
    require(use_prediction or (ranked_patch is None and crop is None), "UNEXPECTED_PREDICTION_HINT")
    return use_prediction, ranked_patch, crop


def observe(directory, receipt_pin, hint):
    use_prediction, ranked_patch, crop = validate_hint(hint)
    require(digest(receipt_pin), "RECEIPT_PIN_SCHEMA")
    request, receipt, reopened_receipt_pin = request_and_receipt(directory)
    require(reopened_receipt_pin == receipt_pin, "RECEIPT_CHANGED")
    artifact, artifact_pin = read(directory, "artifact.json")
    require(artifact_pin == receipt["artifact_sha256"], "ARTIFACT_PIN_MISMATCH")
    shape(artifact, ("identity", "patches"), "ARTIFACT_SCHEMA")
    require(artifact["identity"] == request["identity"], "ARTIFACT_IDENTITY_MISMATCH")
    patches = artifact["patches"]
    require(type(patches) is dict and patches and all(
        identifier(key) and digest(value) for key, value in patches.items()), "PATCH_SCHEMA")
    marker = request["marker"]

    crop_hits = []
    if crop is not None:
        shape(crop, ("identity", "patch_id", "objects"), "CROP_SCHEMA")
        validate_identity(crop["identity"])
        changed = differences(crop["identity"], request["identity"])
        require(not changed, "STALE_CROP:" + ",".join(changed))
        require(ranked_patch in patches and crop["patch_id"] == ranked_patch, "CROP_PATCH_MISMATCH")
        require(pin(encode(crop["objects"])) == patches[ranked_patch], "CROP_CONTENT_MISMATCH")
        crop_hits = patch_hits(crop["objects"], ranked_patch, marker)

    parsed = []

    def reopen(patch_id):
        raw = read_bytes(directory, patch_filename(patch_id))
        require(pin(raw) == patches[patch_id], "PATCH_PIN_MISMATCH")
        return raw

    def inspect_patch(patch_id):
        hits = patch_hits(parse(reopen(patch_id)), patch_id, marker)
        parsed.append(patch_id)
        return hits

    audited_patches, outside_hits, hash_only = [], [], []
    audit_ns, fallback_ns, fallback = None, None, False
    if use_prediction:
        # An independently parsed outside audit protects against off-patch hits
        # and malformed payloads, regardless of the caller's ranking.
        audited_patches = sorted(patches.keys() - {ranked_patch})
        audit_started = monotonic_ns()
        outside_hits = [hit for patch_id in audited_patches for hit in inspect_patch(patch_id)]
        audit_ns = monotonic_ns() - audit_started
        fallback = crop is None or bool(outside_hits)
        if fallback:
            remaining_started = monotonic_ns()
            hits = outside_hits + (inspect_patch(ranked_patch) if ranked_patch in patches else [])
            fallback_ns = monotonic_ns() - remaining_started
        else:
            # Custody still requires the actual file. The validated crop already
            # matches its digest, so skip a second decode and object scan.
            reopen(ranked_patch)
            hash_only.append(ranked_patch)
            hits = crop_hits
    else:
        hits = [hit for patch_id in sorted(patches) for hit in inspect_patch(patch_id)]
    require(len(hits) <= 1, "AMBIGUOUS_TARGET")
    return {
        "status": "reopened",
        "identity": request["identity"],
        "receipt_sha256": reopened_receipt_pin,
        "artifact_sha256": artifact_pin,
        "target": hits[0] if hits else None,
        "audited_patches": audited_patches,
        "outside_patch_miss": bool(ranked_patch and outside_hits),
        "audit_ns": audit_ns,
        "fallback_ns": fallback_ns,
        "fallback": fallback,
        "parsed_fine_patches": sorted(parsed),
        "hash_only_fine_patches": hash_only,
    }


def main(directory, receipt_pin):
    try:
        result = observe(directory, receipt_pin, parse(sys.stdin.buffer.read(LIMIT + 1)))
        rc = 0
    except Refused as exc:
        result, rc = {"status": "refused", "reason": str(exc)}, 1
    print(json.dumps({"reader_pid": os.getpid(), **result}, sort_keys=True))
    return rc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("receipt_sha256")
    args = parser.parse_args()
    sys.exit(main(args.directory, args.receipt_sha256))
