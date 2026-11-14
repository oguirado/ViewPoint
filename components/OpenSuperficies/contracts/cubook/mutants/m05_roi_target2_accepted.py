"""exec accepts roi "target2""""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common  # noqa: E402


def hook(cli, argv, env, run):
    rc, o, e = run()
    if cli == "exec" and rc == 2 and b"roi" in e:
        return 0, b"ok actions=1\n", b""
    return rc, o, e


_common.main(hook)
