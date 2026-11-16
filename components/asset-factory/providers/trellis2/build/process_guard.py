#!/usr/bin/env python3
"""Linux child subreaper for one bounded command, including setsid descendants.

Control JSON goes to an inherited, private descriptor; command stdout is data.
No process names or unowned PIDs are signalled. Requires pidfds and waitid.
"""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def release_children():
    deadline = time.monotonic() + 2  # Separate cleanup budget [TARGET].
    children = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
    while True:
        owned = [int(value) for value in children.read_text().split()]
        if not owned:
            return
        for pid in owned:
            # A direct child cannot have its PID recycled until we reap it.
            try:
                fd = os.pidfd_open(pid)
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
            except ProcessLookupError:
                pass
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
        # Orphaned descendants, including separate sessions, are adopted here.
        if time.monotonic() >= deadline:
            raise TimeoutError("cleanup_unproven")
        time.sleep(0.01)


def supervise(argv, timeout_s):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER, Linux.
        return {"rc": 3, "reason": "supervision_unavailable", "returncode": None}
    command = None
    result = {"rc": 2, "reason": "command_invalid", "returncode": None}
    try:
        command = subprocess.Popen(argv, close_fds=True)
        deadline = time.monotonic() + timeout_s
        while True:
            observed = os.waitid(os.P_PID, command.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            if observed is not None:
                rc = observed.si_status if observed.si_code == os.CLD_EXITED else -observed.si_status
                command.returncode = rc  # Reaping is performed by release_children.
                result = {"rc": 0, "reason": None, "returncode": rc}
                break
            if time.monotonic() >= deadline:
                result = {"rc": 6, "reason": "deadline_exceeded", "returncode": None}
                break
            time.sleep(0.01)
    finally:
        try:
            release_children()
        except Exception:
            result = {"rc": 3, "reason": "cleanup_unproven", "returncode": None}
        if command is not None and command.returncode is None:
            command.returncode = -signal.SIGKILL
    return result


def main():
    fd = None
    try:
        fd = int(sys.argv[2])
        result = supervise(sys.argv[3:], int(sys.argv[1]))
    except Exception:
        result = {"rc": 2, "reason": "supervisor_error", "returncode": None}
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
    if fd is None:
        print(encoded, end="")
        return 2
    try:
        with os.fdopen(fd, "w") as control:
            control.write(encoded)
    except OSError:
        print(encoded, end="")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
