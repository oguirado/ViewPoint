"""Supervise one fixture browser, including helpers that detach from it."""

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


PR_SET_CHILD_SUBREAPER = 36
CHILDREN = Path("/proc/thread-self/children")
DRAIN_TIMEOUT = 3


def drain(timeout=DRAIN_TIMEOUT):
    # Orphaned descendants are reparented here even if they change sessions.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        signalled = set()
        deadline = time.monotonic() + timeout
        while True:
            try:
                while pid := os.waitpid(-1, os.WNOHANG)[0]:
                    signalled.discard(pid)
            except ChildProcessError:
                return

            # These are our direct children. Exits between this read and kill
            # leave zombies whose PIDs cannot be reused until we reap again.
            children = {int(pid) for pid in CHILDREN.read_text().split()}
            for pid in children - signalled:
                os.kill(pid, sig)
            signalled.update(children)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
    raise RuntimeError("owned Chrome process tree did not exit")


def main(argv):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    browser = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    try:
        print(browser.pid, flush=True)
        # The worker closes the pipe at teardown or when it exits.
        sys.stdin.buffer.read(1)
    finally:
        drain()
    browser.poll()


if __name__ == "__main__":
    main(sys.argv[1:])
