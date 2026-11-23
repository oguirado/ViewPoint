"""Build or check the U6b registration manifest from a C1 registry snapshot."""

import argparse
import json
from pathlib import Path
import sys


if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from control.core.control import CapabilityRegistry, canonical_bytes
from expose._publication import publish_manifest
from expose.mcp.tools import registration_manifest


MANIFEST = Path(__file__).with_name("registration_manifest.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--registry", type=Path, help="JSON array of provisioned C1 capability entries")
    source.add_argument("--catalog-only", action="store_true", help="Use C1's frozen adapter catalog alone")
    parser.add_argument("--check", action="store_true", help="Verify that the manifest matches without writing")
    args = parser.parse_args(argv)
    if args.catalog_only:
        registry = CapabilityRegistry()
    else:
        entries = json.loads(args.registry.read_bytes())
        if not isinstance(entries, list):
            parser.error("the registry must be an array of C1 capability entries")
        registry = CapabilityRegistry(entries)
    data = canonical_bytes(registration_manifest(registry))
    if args.check:
        if not MANIFEST.exists() or MANIFEST.read_bytes() != data:
            print(f"Registration manifest is stale: {MANIFEST}", file=sys.stderr)
            return 1
        return 0

    publish_manifest(MANIFEST, data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
