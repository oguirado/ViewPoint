"""Deadline for trusted Python work, without creating a disposable process."""
import signal
import subprocess
import threading
import time
from asset_factory.proc import SignalRefusal


def run(function, timeout_s, *args, **kwargs):
    name = getattr(function, '__name__', type(function).__name__)
    if timeout_s <= 0:
        raise subprocess.TimeoutExpired(name, timeout_s)
    if threading.current_thread() is not threading.main_thread():
        raise SignalRefusal('deadline_unavailable')
    prior = signal.getitimer(signal.ITIMER_REAL)
    # An earlier enclosing deadline already bounds this operation.
    if prior[0] and prior[0] <= timeout_s:
        return function(*args, **kwargs)
    handler = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    def expired(signum, frame):
        raise subprocess.TimeoutExpired(name, timeout_s)
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        return function(*args, **kwargs)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, handler)
        if prior[0]:
            signal.setitimer(signal.ITIMER_REAL, max(.000001, prior[0]-(time.monotonic()-started)), prior[1])
