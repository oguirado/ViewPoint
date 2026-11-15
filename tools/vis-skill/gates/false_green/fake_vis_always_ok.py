#!/usr/bin/env python3
"""Fake vis that answers ok to everything.

Used only to prove the false-green gate is not dead: running
gates/false_green.py against this fake MUST report failures.
Stdlib only.
"""
import json
import sys


def main(argv):
    verb = argv[1] if len(argv) > 1 else "none"
    # Strip leading dashes: vis verbs are bare words.
    if verb.startswith("-"):
        verb = "none"
    out = {"ok": True, "rc": 0, "code": "OK", "verb": verb}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
