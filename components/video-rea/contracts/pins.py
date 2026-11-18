# SPDX-License-Identifier: AGPL-3.0-only
"""Verify canonical JSON hashes for the frozen schemas and the operation table."""

import argparse
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
TABLE = "vp_rea_call.op_table.json"


def unique_keys(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError(f"duplicate JSON key {key!r}")
        values[key] = value
    return values


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)


def canonical_digest(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256((text + "\n").encode("utf-8")).hexdigest()


def check(root):
    try:
        manifest = load_json(root / "PINS.json")
        if manifest["schema"] != "rea.contract_pins/v1" or not isinstance(manifest["files"], dict):
            raise ValueError("invalid pin manifest")
        names = {path.name for path in root.glob("*.schema.json")} | {TABLE}
        if names != manifest["files"].keys() or len(names) == 1:
            raise ValueError("schema or operation table inventory drift")
        for name in sorted(names):
            if canonical_digest(load_json(root / name)) != manifest["files"][name]:
                raise ValueError(f"canonical sha256 mismatch in {name}")
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"pin check failed: {error}", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", required=True, action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT, help="check a staged copy of the contract")
    sys.exit(check(parser.parse_args().root))

