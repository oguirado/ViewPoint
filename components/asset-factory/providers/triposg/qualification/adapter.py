"""In-memory foreground preparation for the TripoSG and PartCrafter pipelines."""
from io import BytesIO

from gates.input_d18 import inspect_matte, qualify_input
from tools.input.matte import read_intent
from tools.input.evidence import InputIssue, bytes_digest, is_digest, require


def preprocess(*, original, straight_alpha, submitted, submitted_digest, approved_mask,
               intent_digest, profile_digest, receipt_digest, read_checked, birefnet_qualification=None):
    """Return white-composited RGB using the independently admitted INPUT crop.

    The caller supplies the protected INPUT pins and role-aware reader. This handoff
    accepts image bytes only and never invokes a segmentation or provider pipeline.
    """
    require(submitted is not None, 3, "submitted_rgb_bytes_missing")
    require(type(submitted) is bytes, 2, "invalid_submitted_rgb_bytes")
    require(is_digest(submitted_digest), 2, "invalid_submitted_pin")
    require(bytes_digest(submitted) == submitted_digest, 4, "submitted_pin_drift")

    intent = read_intent(read_checked, intent_digest)
    native = inspect_matte(original, straight_alpha, submitted, approved_mask,
                           intent_digest=intent_digest, read_checked=read_checked,
                           matte_source=intent["matte_source"], birefnet_qualification=birefnet_qualification)
    verdict = qualify_input(native, profile_digest=profile_digest, receipt_digest=receipt_digest,
                            read_checked=read_checked)
    if verdict["rc"] != 0:
        raise InputIssue(verdict["rc"], verdict["reasons"][0])

    from PIL import Image, ImageChops, ImageOps

    geometry = native["generator"]
    resize = tuple(geometry["resize"])
    with Image.open(BytesIO(straight_alpha), formats=("PNG",)) as master:
        # INPUT resizes RGBA before extracting alpha. Use the same raster and crop,
        # including transparent pixels where the crop extends past the raster.
        resized = master.resize(resize, Image.Resampling.LANCZOS) if master.size != resize else master
        alpha = resized.getchannel("A").crop(tuple(geometry["crop"]))
    background = ImageOps.invert(alpha).convert("RGB")
    with Image.open(BytesIO(submitted), formats=("PNG",)) as foreground:
        # INPUT already supplied RGB * alpha. Add white's contribution once.
        return ImageChops.add(foreground, background)
