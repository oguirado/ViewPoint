"""snap/wlcap accept an unmanaged ramws dir"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common  # noqa: E402


def hook(cli, argv, env, run):
    rc, o, e = run()
    return (0 if rc == 5 and b"unmanaged_run_dir" in e else rc), o, e


_common.main(hook)
