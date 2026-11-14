# @module {"id":"wa.tools.anchors","layer":0,"owner":"ARCH","deps":[],"book":"HB-WA-a0-arch"}
"""@module wa.tools.anchors - list code anchors (`@anchor:<id>`) and module headers. Stdlib only.

Usage: python3 tools/anchors.py [--q WORD] [--json]
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_GLOBS = ("wender/**/*.py", "js/inpage/*.js", "tools/*.py", 'tests/scripts/test_routing_integration.py')
ANCHOR_RE = re.compile(r"^\s*(?:#|//) @anchor:([a-z][a-z0-9_.A-Z]*)\s*$")
MODULE_RE = re.compile(r"^(?:#|//) @module (\{.*\})\s*$")


# @anchor:wa.tools.anchors.source_files
def source_files():
    out = []
    for g in SRC_GLOBS:
        out += [p for p in ROOT.glob(g) if p.name != "__init__.py"]
    return sorted(out)


# @anchor:wa.tools.anchors.scan
def scan():
    anchors, modules = [], {}
    for p in source_files():
        lines = p.read_text(encoding="utf-8").splitlines()
        rel = str(p.relative_to(ROOT))
        m = MODULE_RE.match(lines[0]) if lines else None
        modules[rel] = json.loads(m.group(1)) if m else None
        for i, line in enumerate(lines, 1):
            a = ANCHOR_RE.match(line)
            if a:
                anchors.append({"id": a.group(1), "file": rel, "line": i})
    return anchors, modules


def main(argv):
    q = argv[argv.index("--q") + 1].lower() if "--q" in argv else None
    anchors, _ = scan()
    rows = [a for a in anchors if q is None or q in a["id"].lower()]
    if "--json" in argv:
        print(json.dumps(rows, sort_keys=True, separators=(",", ":")))
    else:
        for a in rows:
            print("%s  %s:%d" % (a["id"], a["file"], a["line"]))
    return 0 if rows else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
