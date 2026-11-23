"""Run an offline qualification attempt and emit the retained receipt."""

import argparse
from pathlib import Path

from asset_factory import core
from .harness import qualify
from .manifest import JSON_LIMIT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("evidence_dir", type=Path)
    parser.add_argument("--timeout-s", type=int, required=True)
    args = parser.parse_args()
    try:
        candidate = core.load_json(args.manifest, max_bytes=JSON_LIMIT)
        receipt = qualify(candidate, args.evidence_dir, timeout_s=args.timeout_s)
    except (core.ContractError, OSError, ValueError) as exc:
        print(core.canonical_json({"rc": 2, "reason": getattr(exc, "code", type(exc).__name__)}), end="")
        return 2
    return core.emit(receipt)


if __name__ == "__main__":
    raise SystemExit(main())
