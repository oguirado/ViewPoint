"""exec replay stdout loses its final newline"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common  # noqa: E402


def hook(cli, argv, env, run):
    rc, o, e = run()
    if cli == "exec" and argv[:1] == ["replay"]:
        o = o.rstrip(b"\n")
    return rc, o, e


_common.main(hook)
