"""wlcap honours --fake-fb without OS_CU_TEST=1"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common  # noqa: E402


def hook(cli, argv, env, run):
    if cli == "wlcap":
        env["OS_CU_TEST"] = "1"
    return run(None, env)


_common.main(hook)
