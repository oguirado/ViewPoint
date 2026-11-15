"""exec rc 2 -> 1 (SPEC_INT R4 forbids 1)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common  # noqa: E402


def hook(cli, argv, env, run):
    rc, o, e = run()
    return (1 if cli == "exec" and rc == 2 else rc), o, e


_common.main(hook)
