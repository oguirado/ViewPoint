"""Private-staging ORM assembly; all image algorithms are imported from vqa3d."""
from .bake_support import Refusal, read_bytes, require_receipt, sha, upstream


def verify_channels(ao, roughness, metal, packed):
    """Round-trip every byte against the imported linear channel packer.

    The comparison detects swapped channels and transfer-function changes; it
    does not certify the sampling or materials of a subsequently assembled GLB.
    """
    tool = upstream("orm_pack")
    maps = [tool.decode_png(read_bytes(p)) for p in (ao, roughness, metal)]
    dims = {(w, h) for w, h, _, _ in maps}
    if len(dims) != 1:
        raise Refusal(2, "map_dimensions_differ")
    w, h = next(iter(dims))
    planes = [pixels[::channels] for _, _, channels, pixels in maps]
    # Independent channel contract, rather than asking the producer to verify itself.
    floor = tool.ASSET_ORM_ROUGH_MIN_BYTE
    clamped = sum(v < floor for v in planes[1])
    data = read_bytes(packed)
    ow, oh, ch, actual = tool.decode_png(data)
    if (ow, oh, ch) != (w, h, 4) or any(
            actual[i * 4:i * 4 + 4] != bytes((planes[0][i], max(planes[1][i], floor), planes[2][i], 255))
            for i in range(w * h)):
        raise Refusal(5, "orm_channel_drift")
    if tool.validate_orm(data)[0]:
        raise Refusal(5, "orm_floor")
    return {"pixels_checked": w * h, "roughness_clamped_pixels": clamped,
            "channels": ["occlusion", "roughness", "metallic"], "color_space": "linear"}


def pack_maps(staging, *, previous):
    """Only called on an owned fresh staging directory by bake.bake_bundle."""
    tool = upstream("orm_pack")
    ao, rough, metal = [staging / (n + ".png") for n in ("ao", "roughness", "metal")]
    before = [read_bytes(p) for p in (ao, rough, metal)]
    prev = sha(read_bytes(previous))
    args = ["--ao", str(ao), "--rough", str(rough), "--metal", str(metal),
            "--out", str(staging / "orm.png"), "--prev-receipt", str(previous)]
    rc, receipt = tool.run(args)
    require_receipt(rc, receipt, tool.SCHEMA, link="prev_sha256", previous=prev)
    if receipt.get("input_sha256") != sha(b"\0".join(before)):
        raise Refusal(4, "orm_input_drift")
    if receipt.get("out_sha256") != sha(read_bytes(staging / "orm.png")):
        raise Refusal(4, "orm_output_drift")
    metrics = verify_channels(ao, rough, metal, staging / "orm.png")
    return receipt, metrics
