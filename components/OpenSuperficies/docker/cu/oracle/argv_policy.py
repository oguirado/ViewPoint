#!/usr/bin/env python3
"""argv refusal policy for the oracle (W6a FX1, ADC-W6A-07). Table: argv_refusal.json (same directory); the shim bin/chromium
carries the same one-line check (it must stay self-contained). Normalisation: lower-case, '_' -> '-'."""
NEEDLE = "remote-debugging"
PIPE = "--remote-debugging-pipe"


def _hit(a):
    return NEEDLE in a.lower().replace("_", "-")


def refuse_input(args):
    """True iff the argv handed to the shim must be refused (rc 2): ANY spelling of a remote-debugging switch, pipe included."""
    return any(_hit(a) for a in args)


def observed_ok(argv):
    """True iff the final chromium argv has exactly one bare --remote-debugging-pipe and no other remote-debugging switch."""
    return argv.count(PIPE) == 1 and sum(_hit(a) for a in argv) == 1
