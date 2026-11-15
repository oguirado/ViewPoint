#!/usr/bin/env python3
"""Test double of osb: records argv to $FAKE_OSB_LOG, enforces the refute rule (rc 2)."""
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_OSB_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")
if "refute" in argv and not any(a.startswith("--ref") for a in argv):
    sys.exit(2)
print(json.dumps({"id": "fake", "lamport": 1}))
