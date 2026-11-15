"""snap stdout receipt gains an extra field"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common  # noqa: E402


def hook(cli, argv, env, run):
    import json
    rc, o, e = run()
    if cli == "snap" and rc == 0:
        d = json.loads(o); d["extra"] = 1; o = (json.dumps(d, separators=(",", ":")) + "\n").encode()
    return rc, o, e


_common.main(hook)
