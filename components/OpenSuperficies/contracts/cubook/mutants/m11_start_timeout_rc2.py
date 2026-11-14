"""exec WL start timeout is rc 2 instead of rc 5"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common  # noqa: E402


def hook(cli, argv, env, run):
    rc, o, e = run()
    return (2 if cli == "exec" and rc == 5 and b"start_timeout" in e else rc), o, e


_common.main(hook)
