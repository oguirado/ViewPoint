# SPDX-License-Identifier: AGPL-3.0-only
"""Fixture program for stage parent and independently bound evidence readback."""
from hashlib import sha256
import json
from pathlib import Path
import sys

if __name__ == "__main__":
    operation, *args = sys.argv[1:]
    if operation == "copy":
        src, dst = map(Path, args)
        dst.write_bytes(src.read_bytes())
    elif operation == "write":
        dst, word = args
        Path(dst).write_text(word + "\n", encoding="utf-8")
    elif operation == "evidence":
        dest, stage, plan_sha, raw_parents = args
        path = Path(dest)
        data = b"independent fixture evidence\n"
        (path.parent / "sample.txt").write_bytes(data)
        record = {"schema": "viewpoint.motion-scene.evidence/v1", "stage": stage,
                  "plan_sha256": plan_sha, "parents": json.loads(raw_parents),
                  "files": {"sample": {"path": "sample.txt", "sha256": sha256(data).hexdigest()}}}
        path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    else:
        sys.exit(2)
