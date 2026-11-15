# @module {"id":"wa.tools.reseal","layer":0,"owner":"ARCH","deps":[],"book":"HB-WA-a0-arch"}
"""@module wa.tools.reseal - (re)write hyperbooks/<book>/manifest.json with sha256 of the book files.

Usage: python3 tools/reseal.py [--check]   (--check: rc 4 if any manifest is stale, writes nothing)
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HB = ROOT / "hyperbooks"


# @anchor:wa.tools.reseal.manifest_for
def manifest_for(book_dir):
    files = {}
    for p in sorted(book_dir.iterdir()):
        if p.is_file() and p.name != "manifest.json":
            files[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return {"schema": "hb.manifest.v2", "hb_id": book_dir.name, "files": files}


def dump(obj):
    return json.dumps(obj, sort_keys=True, indent=1) + "\n"


def main(argv):
    fmt = json.loads((HB / "COMPANION_FORMAT.json").read_text())
    stale = []
    for name in fmt["expected_books"]:
        d = HB / name
        want = dump(manifest_for(d))
        mp = d / "manifest.json"
        have = mp.read_text() if mp.exists() else None
        if have != want:
            stale.append(name)
            if "--check" not in argv:
                mp.write_text(want)
    print(json.dumps({"stale": stale, "check": "--check" in argv}, separators=(",", ":")))
    return 4 if (stale and "--check" in argv) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
