# SPDX-License-Identifier: AGPL-3.0-only
"""Isolated launcher for the local production capability."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from studio import emit, installed_registry, refusal


if __name__ == "__main__":
    try:
        installed_registry()
    except (OSError, ValueError, RuntimeError, ImportError, KeyError) as exc:
        emit(refusal("registry_stale", "after reinstalling the complete pinned release",
                     ["viewpoint-studio", "--episode", *sys.argv[1:]], str(exc)))
        sys.exit(1)
    from viewpoint import handlers
    from viewpoint.episode_local import common, entry

    if sys.argv[1:] == ["--list-capabilities"]:
        sys.stdout.buffer.write(common.encoded(entry.capabilities()))
    else:
        sys.exit(handlers.main(["--execute", *sys.argv[1:]]))
