"""Print one read-only resource inventory."""

import argparse
import json
from pathlib import Path
import sys

if __package__:
    from .inventory import IncompleteInventory, collect
else:
    # Support an isolated source-checkout launch via the package directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from baseline.inventory import IncompleteInventory, collect


def _scope(argument):
    owner, separator, cgroup = argument.partition("=")
    if not separator or not owner.strip() or not cgroup:
        raise argparse.ArgumentTypeError("expected OWNER=/absolute/cgroup")
    return owner, cgroup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", required=True, help="emit one JSON object")
    parser.add_argument("--scope", action="append", type=_scope, metavar="OWNER=CGROUP")
    args = parser.parse_args(argv)
    try:
        inventory = collect(args.scope)
    except IncompleteInventory as error:
        parser.exit(2, f"incomplete inventory: {error}\n")
    print(json.dumps(inventory, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
