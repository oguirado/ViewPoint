"""Generate the mvctl registration manifest from a C1 capability snapshot."""

import argparse
import json
from pathlib import Path
import sys

from jsonschema import ValidationError


if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from control.core.control import CapabilityRegistry, canonical_bytes
from expose._publication import publish_manifest
from expose.cli.verbs import registration_manifest


MANIFEST = Path(__file__).with_name("registration_manifest.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--registry", type=Path, help="JSON array of provisioned C1 capability entries")
    source.add_argument("--catalog-only", action="store_true", help="Project the frozen adapter catalog")
    parser.add_argument("--check", action="store_true", help="Verify the manifest without writing it")
    args = parser.parse_args(argv)

    try:
        entries = [] if args.catalog_only else json.loads(args.registry.read_bytes())
        if not isinstance(entries, list):
            raise ValueError("expected a JSON array of capabilities")
        data = canonical_bytes(registration_manifest(CapabilityRegistry(entries)))
    except (OSError, ValueError, ValidationError) as exc:
        parser.error(str(exc))

    if args.check:
        if not MANIFEST.exists() or MANIFEST.read_bytes() != data:
            print("CLI registration manifest is stale", file=sys.stderr)
            return 1
        return 0

    publish_manifest(MANIFEST, data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
