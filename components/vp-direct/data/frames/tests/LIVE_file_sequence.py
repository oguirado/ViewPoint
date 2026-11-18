"""LIVE: verify the file fallback with a supplied render and decoded video frame."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS.parents[1]))

from frames import FileFrameSequence, FrameSequenceError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path,
                        help="provisioned scratch directory")
    parser.add_argument("--render-frame", required=True, type=Path)
    parser.add_argument("--video-frame", required=True, type=Path,
                        help="an encoded frame extracted from operator-owned video")
    parser.add_argument("--max-frame-bytes", required=True, type=int)
    args = parser.parse_args()
    if args.max_frame_bytes <= 0:
        parser.error("--max-frame-bytes must be positive")

    fixture = _TESTS.parents[2] / "schemas/tests/examples/vp.surface_binding_v1.valid.json"
    binding = json.loads(fixture.read_bytes())
    owner = binding["owner"]
    sequence = FileFrameSequence(binding, directory=args.directory,
                                 slots=1, max_frame_bytes=args.max_frame_bytes)
    output = []
    try:
        for path in (args.render_frame, args.video_frame):
            with path.open("rb") as source:
                data = source.read(args.max_frame_bytes + 1)
            frame = sequence.append(owner, data)
            with sequence.borrow(owner, frame.index) as borrow:
                if borrow.read() != data or frame.sha256 != sha256(data).hexdigest():
                    raise SystemExit("frame file fallback changed the input")
                if frame.copy != "file":
                    raise SystemExit("frame file fallback copy mode is missing")
                output.append({"index": frame.index, "bytes": frame.size_bytes,
                               "sha256": frame.sha256, "copy": frame.copy})

        try:
            with sequence.borrow(owner + "-foreign", frame.index):
                raise SystemExit("a foreign owner borrowed the frame")
        except FrameSequenceError:
            pass

        with sequence.borrow(owner, frame.index) as borrow:
            if sequence.close(owner) or sequence.status(owner).retained_bytes != args.max_frame_bytes:
                raise SystemExit("storage settled before borrow drain")
            if borrow.read() != data:
                raise SystemExit("the retained borrow changed during drain")
    finally:
        if not sequence.close(owner) or sequence.status(owner).reserved_bytes:
            raise SystemExit("frame storage settlement is incomplete")
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
