"""LIVE: resolve an operator-selected night-corpus GLB using its pinned identity."""

import argparse
import json
from pathlib import Path, PurePosixPath
import sys
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from locator import ArtifactLocator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--relative-path", required=True)
    parser.add_argument("--sha256", required=True, help="digest from an independent artifact record")
    parser.add_argument("--bytes", required=True, type=int, help="byte count from the artifact record")
    parser.add_argument("--stage", required=True)
    args = parser.parse_args()
    path = PurePosixPath(args.relative_path)
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".glb":
        parser.error("--relative-path must name a GLB under --root")
    locator = "af://night/" + quote(args.relative_path)
    ref = {
        "schema": "vp.artifact_ref/v1",
        "locator": locator,
        "sha256": args.sha256,
        "bytes": args.bytes,
        "media_type": "model/gltf-binary",
        "stage": args.stage,
        "copy": "none",
    }
    store = ArtifactLocator({"night": args.root})
    stored = store.handle({"op": "put", "artifact_ref": ref})
    resolved = store.handle({"op": "resolve", "locator": locator, "sha256": args.sha256})
    if stored != ref or resolved != ref:
        raise SystemExit("D2 live receipt does not match the pinned reference")
    print(json.dumps(resolved, sort_keys=True))


if __name__ == "__main__":
    main()
