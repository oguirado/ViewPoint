"""Replay a pinned ACT fixture and emit its canonical receipt."""
import argparse
from pathlib import Path
import sys

from asset_factory import core
from . import action, replay


def read_bytes(path):
    with path.open("rb") as stream:
        return stream.read(action.MAX_ACTION_BYTES + 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("action", "skin", "anatomy", "skin-receipt", "registration"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--support-plane", type=Path)
    parser.add_argument("--registration-sha256", required=True)
    parser.add_argument("--fps", type=int, nargs=2, required=True, metavar=("NUM", "DEN"))
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = core.load_json(args.registration, max_bytes=action.MAX_ACTION_BYTES)
        receipt, trace = replay.evaluate(
            read_bytes(args.action), read_bytes(args.skin), read_bytes(args.anatomy),
            read_bytes(args.skin_receipt), plan, args.registration_sha256, args.fps,
            support_data=read_bytes(args.support_plane) if args.support_plane is not None else None,
        )
        if args.trace is not None and trace is not None:
            with args.trace.open("x", encoding="utf-8", newline="\n") as output:
                output.write(core.canonical_json(trace))
    except (OSError, core.ContractError) as exc:
        print(f"ACT evidence I/O refused ({type(exc).__name__})", file=sys.stderr)
        return 2
    return core.emit(receipt)


if __name__ == "__main__":
    raise SystemExit(main())
