"""CPU-only fixture with an optional swallowed denial of an outbound connect."""

import errno
import json
from pathlib import Path
import socket
import sys


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text())
    request = json.loads(Path(manifest["request"]["path"]).read_text())
    output = Path(sys.argv[2])
    if request["try_fetch"]:
        with socket.socket() as sock:
            try:
                # TEST-NET-1 literal. The offline launcher denies connect before
                # the kernel can send any traffic, with no DNS involved.
                sock.connect(("192.0.2.1", 443))
            except OSError as exc:
                if exc.errno != errno.EACCES:
                    raise
            else:
                raise AssertionError("offline connection unexpectedly succeeded")
    (output / "result.json").write_text('{"fixture":true}\n')
    (output / "artifact.bin").write_bytes(b"QUAL CPU fixture\n")


if __name__ == "__main__":
    main()
