"""LIVE operator check for locally supplied pins and installed boot artifacts."""

import argparse
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from imagepack import ImagePackCatalogue, ImagePackError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trusted-pack", type=Path, required=True,
                        help="Operator-reviewed vp.image_pack/v1 JSON with absolute pins")
    parser.add_argument("--candidate", type=Path, required=True,
                        help="vp.image_pack/v1 JSON to check against the trusted pack")
    options = parser.parse_args()
    try:
        trusted = json.loads(options.trusted_pack.read_text(encoding="utf-8"))
        candidate = json.loads(options.candidate.read_text(encoding="utf-8"))
        catalogue = ImagePackCatalogue(trusted)
        listed = catalogue.handle({"op": "pack_list"})
        validated = catalogue.handle({"op": "pack_validate", "pack": candidate})
        if listed != {"packs": [validated]}:
            raise ImagePackError("listed pack differs from the validated pack")
        print("PASS notepad-vm image pack")
    except ImagePackError as error:
        parser.exit(1, f"FAIL {error}\n")
    except (OSError, ValueError):
        parser.exit(1, "FAIL unable to read image pack JSON\n")


if __name__ == "__main__":
    main()
