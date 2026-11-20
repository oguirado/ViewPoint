# SPDX-License-Identifier: AGPL-3.0-only
"""Local CLI for a W1 frame export."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from .inputs import DirectoryInputs
from .provider import segment


class LocalSink:
    def __init__(self, destination):
        self.destination = Path(destination)

    def write(self, name, data):
        if not name or name in {".", ".."} or name != Path(name).name:
            raise ValueError("sink requires a basename")
        destination = self.destination / name
        self.destination.mkdir(exist_ok=False)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.destination, delete=False) as stream:
                tmp = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            tmp.replace(destination)
        except BaseException:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            self.destination.rmdir()
            raise
        return name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--out", required=True)
    parser.add_argument("--tessdata-dir")
    args = parser.parse_args(argv)
    options = {"op": "rea.segment"}
    if args.tessdata_dir is not None:
        options["tessdata_dir"] = args.tessdata_dir
    try:
        result = segment(options, DirectoryInputs(args.manifest), LocalSink(args.out))
    except (KeyError, RuntimeError, OSError, TypeError, ValueError) as error:
        parser.exit(1, f"segment refused: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
