"""Finish a fresh scratch Blender capture with an explicit alpha>=128 mask."""
import argparse
import io
from pathlib import Path

from asset_factory import core
from gates.rank.evaluate import Evidence, MAX_IMAGE, MAX_JSON
from gates.rank.metrics import RankError, require


def finalize(directory):
    from PIL import Image
    directory = Path(directory).resolve()
    raw = core.load_json(directory / "capture.json", max_bytes=MAX_JSON)
    require(raw["schema"] == "af.rank-capture-raw/v1" and type(raw["rc"]) is int and raw["rc"] == 0,
            2, "capture_contract")
    image_bytes = Evidence(str(directory)).read({"path": "front.png", "sha256": raw["render_sha256"]}, MAX_IMAGE)
    image = Image.open(io.BytesIO(image_bytes))
    require(image.mode == "RGBA" and image.size == (raw["capture"]["width"], raw["capture"]["height"]),
            2, "capture_image_invalid")
    mask = image.getchannel("A").point(lambda a: 255 if a >= 128 else 0)
    require(mask.getbbox() is not None, 3, "empty_silhouette")
    mask_path, receipt_path = directory / "mask.png", directory / "capture-receipt.json"
    require(not mask_path.exists() and not receipt_path.exists(), 2, "capture_output_exists")
    temporary = directory / ".mask.tmp.png"
    try:
        mask.save(temporary)
        temporary.replace(mask_path)
        record = {"schema": "af.rank-capture/v1", "rc": 0,
                  "input_sha256": raw["input_sha256"], "render_sha256": raw["render_sha256"],
                  "mask_sha256": core.sha256_file(mask_path),
                  "capture_sha256": core.sha256_json(raw["capture"]),
                  "capture_raw_sha256": core.sha256_file(directory / "capture.json"),
                  "renderer_sha256": raw["renderer_sha256"],
                  "mask_policy": "alpha_ge_128", "asset_verdict": "CANNOT_SEE"}
        core.atomic_write_json(receipt_path, record)
        return record
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    from gates.rank.rank import Parser
    parser = Parser()
    parser.add_argument("--capture", required=True, type=Path)
    try:
        result = finalize(parser.parse_args(argv).capture)
    except RankError as exc:
        result = {"rc": exc.rc, "reason_codes": [exc.reason]}
    except core.ContractError as exc:
        result = exc.as_dict()
    except (OSError, ValueError, KeyError, ImportError):
        result = {"rc": 2, "reason_codes": ["capture_invalid"]}
    print(core.canonical_json(result), end="")
    return result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
