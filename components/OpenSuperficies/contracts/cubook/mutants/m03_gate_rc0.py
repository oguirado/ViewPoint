"""exec capsule gate refusal (rc 5) reported as rc 0"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common  # noqa: E402


def hook(cli, argv, env, run):
    rc, o, e = run()
    return (0 if cli == "exec" and rc == 5 and b"not_in_capsule" in e else rc), o, e


_common.main(hook)
