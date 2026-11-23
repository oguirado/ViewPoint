"""Recognize the component checkout, editable tree, or unpacked source archive."""

import os
from pathlib import Path
import stat


SOURCE_MARKER = "SOURCE_LAYOUT"
_MARKER_BYTES = b"blender-visual-qa-source-v1\n"


def is_source_layout(root, *, dir_fd=None):
    marker = Path(root) / SOURCE_MARKER
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or stream.read(len(_MARKER_BYTES) + 1) != _MARKER_BYTES:
            raise OSError(f"Invalid Blender visual QA source marker: {marker}")
    return True
