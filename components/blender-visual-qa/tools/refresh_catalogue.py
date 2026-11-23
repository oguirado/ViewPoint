#!/usr/bin/env python3
"""Compile Blender's six-column registry without modifying the pinned engine compiler."""
# @module {"id":"tools.refreshcatalogue","layer":4,"owner":"BINT"}
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
ANCHOR = re.compile(r"^# @anchor:((?:bv|bpy|tools)(?:\.[A-Za-z0-9_]+){2,})\s*$")


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def build(root):
    cat = root / "catalogue"
    out = {name + ".json": canon(json.loads((cat / (name + ".src")).read_text())) for name in ("subjects", "layers", "measurements")}
    for name, width in (("routes", 3), ("thresholds", 6), ("failures", 6)):
        lines = (cat / (name + ".src")).read_text().splitlines()
        header, *body = lines
        if any(len(s.split("\t")) != width for s in lines):
            raise ValueError(name + ":column_count")
        out[name + ".tsv"] = header + "\n" + "".join(s + "\n" for s in sorted(body) if s)
    symbols = []
    seen = set()
    for folder in ("blender_visual_qa", "bpy_side", "tools"):
        for p in sorted((root / folder).rglob("*.py")):
            if "__pycache__" in p.parts: continue
            for i, line in enumerate(p.read_text().splitlines(), 1):
                match = ANCHOR.fullmatch(line)
                if match:
                    a = match[1]
                    if a in seen: raise ValueError("duplicate_anchor:" + a)
                    seen.add(a)
                    symbols.append({"anchor": a, "path": p.relative_to(root).as_posix(), "line": i, "symbol": a.rsplit(".", 1)[-1], "layer": 4, "owner": "BINT", "tests": [], "mutants": []})
    routes = [line.split("\t") for line in out["routes.tsv"].splitlines()[1:] if line]
    if any(row[1] not in seen for row in routes): raise ValueError("unresolved_route")
    out["symbols.jsonl"] = "".join(canon(row) for row in sorted(symbols, key=lambda row: row["anchor"]))
    return out


def _check_router(root):
    import subprocess
    res = subprocess.run([sys.executable, "-B", str(root / "catalogue" / "router" / "build_router_catalogue.py"), "--root", str(root), "--check"], capture_output=True, text=True)
    try: detail = json.loads(res.stdout).get("detail")
    except ValueError: detail = res.stdout.strip() or res.stderr.strip() or None
    return res.returncode, detail
# @anchor:tools.refreshcatalogue.main
def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    p.add_argument("--root", type=Path, default=ROOT)
    args = p.parse_args(argv)
    try:
        outputs = build(args.root)
        stale = [name for name, text in sorted(outputs.items()) if not (args.root / "catalogue" / name).is_file() or (args.root / "catalogue" / name).read_text() != text]
        if args.check:
            owner_rc, owner_detail = _check_router(args.root)
            if owner_rc and isinstance(owner_detail, dict):
                stale.extend(owner_detail.get("stale", []))
            rc = 4 if stale else owner_rc
            payload = {"code": "E_STALE" if stale else ("E_ROUTER" if rc else "OK"), "rc": rc, "stale": stale}
            if owner_rc:
                payload["router_rc"] = owner_rc
                if owner_detail is not None:
                    payload["router"] = owner_detail
            print(canon(payload), end="")
            return rc
        for name, text in sorted(outputs.items()):
            target = args.root / "catalogue" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(target.suffix + ".tmp")
            temp.write_text(text)
        for name in sorted(outputs):
            target = args.root / "catalogue" / name
            target.with_suffix(target.suffix + ".tmp").replace(target)
        print(canon({"code": "OK", "rc": 0, "files": sorted(outputs)}), end="")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(canon({"code": "E_SCHEMA", "rc": 2, "reason": str(exc)}), end="")
        return 2


if __name__ == "__main__":
    sys.exit(main())
