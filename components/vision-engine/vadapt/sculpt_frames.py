# @module {"id":"va.sculpt_frames","layer":4,"stage":"ADAPT","owner":"ARCH","deps":["vc.frame","vc.errors"],"inputs":"immutable RAM RGB24 pair and expected decoded hashes","outputs":"MemoryFrameSource and vqa.frame/v1 manifest"}
"""RAM-only sculpt comparison input. No file IO, rendering, or engine algorithms.

Each camera is a separate two-image comparison, not an animation. The caller
must bind the decoded bytes to its renderer and must not treat S2 classes as
temporal flicker measurements. No frame or manifest is published here.
"""
import hashlib

import numpy as np

from visioncore.frame import Frame, MemoryFrameSource, validate_manifest
from visioncore.errors import VqaError


def pair_source(reference, candidate, *, width, height,
                reference_sha256, candidate_sha256):  # @anchor:va.sculpt_frames.pair_source
    """Return (RAM source, validated manifest); hashes are decoded RGB identities.

    Immutable bytes prevent a mutable caller buffer from changing after hashing.
    Dimensions and pixel budget belong to the admitting host, not engine params.
    """
    if (type(width) is not int or type(height) is not int or width < 1 or height < 1
            or type(reference) is not bytes or type(candidate) is not bytes
            or len(reference) != width * height * 3 or len(candidate) != len(reference)):
        raise VqaError("E_VC_FRAME_SHAPE", {"reason": "RAM RGB24 pair"})
    frames, rows = [], []
    for i, (pixels, expected) in enumerate(((reference, reference_sha256),
                                          (candidate, candidate_sha256))):
        if type(expected) is not str or hashlib.sha256(pixels).hexdigest() != expected:
            raise VqaError("E_VC_FRAME_SHA", {"i": i})
        rgb = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3)
        frame = Frame(i, i, i, rgb, expected)
        frames.append(frame)
        rows.append({"i": i, "t_us": i, "render_index": i,
                     "sha256": frame.sha256, "file_sha256": frame.sha256})
    manifest = {"schema": "vqa.frame/v1", "width": width, "height": height,
                "channels": 3, "pixel_format": "rgb24", "count": len(frames),
                "fps_num": 1, "fps_den": 1, "storage": "rgb24_raw",
                "raw_path": "frames.rgb24", "frame_bytes": width * height * 3,
                "frames": rows, "hints": None, "still_probe": None,
                "scope": {"app": "blender", "scene": "sculpt_pair",
                          "res": "%dx%d" % (width, height), "build_seq": None},
                "provenance": {"adapter_id": "sculpt_ram", "adapter_version": "1",
                               "capture_receipt_sha256": None}}
    validate_manifest(manifest)
    return MemoryFrameSource(frames, {k: v for k, v in manifest.items() if k != "frames"}), manifest


def residual_source(reference, candidate, *, width, height,
                    reference_sha256, candidate_sha256):  # @anchor:va.sculpt_frames.residual_source
    """Exact changed-pixel mask -> engine frames; no region algorithm here.

    S2 on ordinary RGB is thresholded and can omit subtle normal changes. This
    declared translation gives S0-S4 a black baseline and a binary RGB witness
    of *every* unequal decoded pixel. Original frame identities are preserved
    separately in the returned binding; mask frames never replace those ids.
    """
    source, original = pair_source(reference, candidate, width=width, height=height,
                                  reference_sha256=reference_sha256,
                                  candidate_sha256=candidate_sha256)
    a, b = list(source)
    changed = np.any(a.rgb != b.rgb, axis=2)
    mask = np.repeat(changed[:, :, None], 3, axis=2).astype(np.uint8) * 255
    black, white = bytes(len(reference)), mask.tobytes()
    translated, manifest = pair_source(black, white, width=width, height=height,
                                       reference_sha256=hashlib.sha256(black).hexdigest(),
                                       candidate_sha256=hashlib.sha256(white).hexdigest())
    return translated, manifest, original
