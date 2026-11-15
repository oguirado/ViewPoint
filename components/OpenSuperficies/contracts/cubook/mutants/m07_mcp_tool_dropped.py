"""mcp tools/list omits cu_wait"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common  # noqa: E402


def hook(cli, argv, env, run):
    rc, o, e = run()
    if cli == "mcp" and b"cu_wait" in o and b"tools" in o:
        import json
        ls = []
        for ln in o.decode().splitlines():
            d = json.loads(ln)
            if "tools" in d.get("result", {}):
                d["result"]["tools"] = [t for t in d["result"]["tools"] if t["name"] != "cu_wait"]
            ls.append(json.dumps(d, sort_keys=True, separators=(",", ":")))
        o = ("\n".join(ls) + "\n").encode()
    return rc, o, e


_common.main(hook)
