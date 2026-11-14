#!/usr/bin/env python3
"""Stands in for the real chromium behind the oracle shim: records argv and the open fd table to $FAKE_OUT."""
import json, os, sys
fds = sorted(int(x) for x in os.listdir("/proc/self/fd") if os.path.exists("/proc/self/fd/" + x))
with open(os.environ["FAKE_OUT"], "w") as f:
    json.dump({"argv": sys.argv[1:], "fds": fds}, f)
sys.exit(0)
