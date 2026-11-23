"""LIVE: put and resolve an operator-selected artifact without the block runtime."""

import argparse
import json
from pathlib import Path, PurePosixPath
import sys
from urllib.parse import quote


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from abi import BlockRuntimeAdapter
from locator import ArtifactLocator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--relative-path", required=True)
    parser.add_argument("--sha256", required=True, help="digest from the artifact record")
    parser.add_argument("--bytes", required=True, type=int, help="byte count from the artifact record")
    parser.add_argument("--media-type", required=True)
    parser.add_argument("--stage", required=True)
    args = parser.parse_args()

    relative = PurePosixPath(args.relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        parser.error("--relative-path must name a file under --root")
    locator = "af://fixture/" + quote(relative.as_posix())
    ref = {
        "schema": "vp.artifact_ref/v1",
        "locator": locator,
        "sha256": args.sha256,
        "bytes": args.bytes,
        "media_type": args.media_type,
        "stage": args.stage,
        "copy": "none",
    }
    store = ArtifactLocator({"fixture": args.root})
    adapter = BlockRuntimeAdapter(store.handle)
    stored = adapter.dispatch({"op": "put", "artifact_ref": ref})
    resolved = adapter.dispatch({"op": "resolve", "locator": locator, "sha256": args.sha256})
    expected = dict(ref, copy="file")
    if stored.result != expected or resolved.result != expected or stored.copy != "file" or resolved.copy != "file":
        raise SystemExit("D1 live receipts disagree with the pinned file fallback")
    print(json.dumps(resolved.result, sort_keys=True))


if __name__ == "__main__":
    main()
