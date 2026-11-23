"""Deterministic interruption at a trusted handler boundary; no OS signalling."""
import functools
import signal


def signal_current(sig):
    handler = signal.getsignal(sig)
    if not callable(handler):
        raise RuntimeError('an installed interruption handler is required')
    handler(sig, None)


def owned_signal_case(method):
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        return method(self, *args, **kwargs)
    return run
