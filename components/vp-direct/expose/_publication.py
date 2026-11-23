"""Atomic registration manifest publication for the exposure generators."""

from fcntl import LOCK_EX, flock
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def publish_manifest(path, data):
    # The directory inode stays stable while each publication replaces the file.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        flock(directory, LOCK_EX)
        with NamedTemporaryFile(dir=path.parent, prefix=".registration-", delete=False) as pending:
            temp = Path(pending.name)
            try:
                pending.write(data)
                pending.flush()
                os.fsync(pending.fileno())
                os.chmod(temp, 0o644)
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
    finally:
        os.close(directory)
