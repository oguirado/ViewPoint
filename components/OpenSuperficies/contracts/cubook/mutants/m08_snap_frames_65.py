"""snap accepts --frames 65 (cap is 64)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common  # noqa: E402


def hook(cli, argv, env, run):
    rc, o, e = run()
    if cli == "snap" and "65" in argv and rc == 2:
        return 0, b"", b""
    return rc, o, e


_common.main(hook)
