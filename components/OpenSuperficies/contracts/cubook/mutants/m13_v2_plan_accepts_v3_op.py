"""exec prereg v2 accepts a v3-only op"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common  # noqa: E402


def hook(cli, argv, env, run):
    rc, o, e = run()
    return (0 if cli == "exec" and rc == 2 and b"prereg-wl/v3" in e else rc), o, e


_common.main(hook)
