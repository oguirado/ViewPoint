"""Check live MicroVM restore availability or explicitly replay a local fixture."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from rewind.microvm import Binding, Phase, replay_restore


FIXTURE = HERE / "restore_fixture.json"


def load_fixture(path=FIXTURE):
    with path.open(encoding="utf-8") as stream:
        trace = json.load(stream)
    return (
        Binding(**trace["previous"]),
        Binding(**trace["following"]),
        tuple(Phase(**phase) for phase in trace["phases"]),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--fixture", type=Path, help="Replay a local fixture.")
    args = parser.parse_args(argv)
    if args.fixture is None:
        print("E_LIVE_BACKEND_ABSENT NOT-DONE", file=sys.stderr)
        return 1

    previous, following, phases = load_fixture(args.fixture)
    result = replay_restore(previous, following, phases)
    print(json.dumps({
        "mode": "local_replay",
        **asdict(result),
        "commands_allowed": result.command_allowed(following),
        "pre_restore_reference_allowed": result.command_allowed(previous),
    }, sort_keys=True))
    return 0 if result.command_allowed(following) else 1


if __name__ == "__main__":
    raise SystemExit(main())
