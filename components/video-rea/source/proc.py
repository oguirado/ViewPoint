# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded binary pipes for trusted component commands in the scoped PID namespace."""
import math
import os
from pathlib import Path
import selectors
import subprocess
import time


class ChildError(ValueError):
    pass


class ResourceLimitError(ValueError):
    """A configured resource cap was exceeded."""


class ChildLimitError(ChildError, ResourceLimitError):
    pass


def _namespace_pids(root):
    for line in (root / "self/status").read_text(encoding="ascii").splitlines():
        if line.startswith("NSpid:"):
            return tuple(map(int, line.split()[1:]))
    raise ChildError("PID namespace evidence unavailable")


def require_namespace():
    outer = Path("/proc/1/fd/9")
    if (os.getenv("AF_PROC_BACKEND") != "pidns" or
            os.getenv("AF_PROC_PIDNS_PROC") != str(outer)):
        raise ChildError("scoped PID namespace required")
    try:
        local = _namespace_pids(Path("/proc"))
        host = _namespace_pids(outer)
        nested = (len(local) == 1 and len(host) > 1 and
                  host[-1:] == local == (os.getpid(),) and
                  os.stat(outer / "1/ns/pid").st_ino != os.stat("/proc/self/ns/pid").st_ino)
    except OSError as exc:
        raise ChildError("unverifiable PID namespace") from exc
    if not nested:
        raise ChildError("scoped PID namespace required")


def run_child(argv, *, timeout=60, max_stdout=8 << 20, max_stderr=1 << 20,
              on_stdout=None, on_stderr=None, cwd=None):
    """Run trusted argv without a shell. Sinks receive bounded binary chunks.

    Return stdout bytes if there is no stdout sink. A sink failure, limit or
    timeout kills and reaps only the child returned by this Popen.
    """
    require_namespace()
    if not math.isfinite(timeout) or timeout <= 0 or min(max_stdout, max_stderr) < 0:
        raise ValueError("invalid child limits")
    out, err = bytearray(), bytearray()
    def stderr(chunk):
        if on_stderr is not None:
            on_stderr(chunk)
        err.extend(chunk[-2048:])
        del err[:-2048]

    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as child:
        try:
            with selectors.DefaultSelector() as pipes:
                pipes.register(child.stdout, selectors.EVENT_READ, (on_stdout or out.extend, max_stdout))
                pipes.register(child.stderr, selectors.EVENT_READ, (stderr, max_stderr))
                sizes = {child.stdout: 0, child.stderr: 0}
                while pipes.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ChildLimitError("CHILD_TIMEOUT: child timeout")
                    for key, _ in pipes.select(remaining):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            pipes.unregister(key.fileobj)
                            continue
                        sink, limit = key.data
                        sizes[key.fileobj] += len(chunk)
                        if sizes[key.fileobj] > limit:
                            pipe = "stdout" if key.fileobj is child.stdout else "stderr"
                            raise ChildLimitError(f"CHILD_OUTPUT_LIMIT: child {pipe} limit exceeded")
                        sink(chunk)
            rc = child.wait(timeout=max(0, deadline - time.monotonic()))
            if rc:
                raise ChildError(f"{Path(argv[0]).name} exited {rc}: {err.decode(errors='replace')}")
        except subprocess.TimeoutExpired as exc:
            raise ChildLimitError("CHILD_TIMEOUT: child timeout") from exc
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
    return bytes(out)
