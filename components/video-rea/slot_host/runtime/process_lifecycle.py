# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded process lifecycle for REA slot host launches (U09, C21/C23-C25).

This module owns supervision of one started child: a single effective
deadline, incremental bounded output collection under a combined
stdout/stderr byte cap, identity-safe teardown, and host-owned scratch
acquisition and removal.

Fail-closed contract:
- A signal is sent only to a pid > 1 whose recorded spawn-time
  starttime is a plain int and still matches the live /proc starttime.
  A missing, bool, None or mismatched identity never signals on the
  bare PID, and no fresh-starttime fallback is taken.
- Output retention never exceeds the combined cap in raw bytes.
  An additional byte distinguishes a full capture from an overflow.
  On overflow the caller terminates the child.
- The deadline comes from the binding timeout already capped by the
  operation limit.  Collection and the reap share that one deadline;
  teardown grace is bounded and never unbounded.
- Scratch removal is observed (the directory is checked to be gone)
  and never silently claimed: callers retain UNCERTAIN_OUTCOME when
  removal is not observed.
- Signals target exactly the bound pid, never a process group, and
  never pid <= 1.  Only pids the caller started and recorded are
  signalled.
"""

from __future__ import annotations

import os
import selectors
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

_READ_CHUNK = 65536
_TEARDOWN_GRACE_S = 1.0
_TEARDOWN_WAIT_S = 2.0


def proc_starttime(pid: int) -> int | None:
    """Live /proc starttime for pid, or None when unavailable or invalid."""
    if type(pid) is not int or pid <= 1:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return int(raw.rsplit(")", 1)[1].split()[19])
    except (FileNotFoundError, PermissionError, IndexError, ValueError, OSError):
        return None


def effective_deadline(timeout_s: float) -> float:
    """The single monotonic deadline for collection and reap."""
    if type(timeout_s) not in (int, float) or timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    return time.monotonic() + float(timeout_s)


def wait_for_exit(proc: subprocess.Popen, deadline: float) -> bool:
    """Observe child exit within the deadline shared with output capture."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    if proc.returncode is not None:
        return True
    try:
        proc.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        return False
    return time.monotonic() <= deadline


def collect_output(
    proc: subprocess.Popen, deadline: float, cap: int
) -> tuple[bytes, bytes, bool, bool]:
    """Incrementally drain stdout/stderr until EOF, deadline, or cap.

    Returns ``(stdout, stderr, timed_out, overflow)``.  Retained bytes
    never exceed *cap* across the two streams. An additional byte is
    observed only to detect overflow; the caller terminates the child on
    overflow or deadline.  A grandchild holding the pipes
    keeps this side open until the deadline, which yields ``timed_out``
    without ever blocking the host.
    """
    if type(cap) is not int or cap < 1:
        raise ValueError("cap must be a positive int")
    out = bytearray()
    err = bytearray()
    pipes = {}
    for stream, buf in ((proc.stdout, out), (proc.stderr, err)):
        if stream is not None:
            try:
                pipes[stream.fileno()] = (stream, buf)
            except (ValueError, OSError):
                pass
    if not pipes:
        return b"", b"", not wait_for_exit(proc, deadline), False
    sel = selectors.DefaultSelector()
    open_fds: set[int] = set(pipes)
    timed_out = False
    overflow = False
    try:
        for fd in pipes:
            os.set_blocking(fd, False)
            sel.register(fd, selectors.EVENT_READ)
        while open_fds:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = sel.select(timeout=min(remaining, 0.05))
            for key, _mask in events:
                fd = key.fd
                if fd not in open_fds:
                    continue
                room = cap - (len(out) + len(err))
                try:
                    stream, buffer = pipes[fd]
                    count = min(_READ_CHUNK, room + 1)
                    chunk = stream.read_chunk(count) if hasattr(stream, "read_chunk") else os.read(fd, count)
                except BlockingIOError:
                    continue
                if not chunk:
                    open_fds.discard(fd)
                    try:
                        sel.unregister(fd)
                    except Exception:
                        pass
                    continue
                if len(chunk) > room:
                    if room > 0:
                        buffer.extend(chunk[:room])
                    overflow = True
                    open_fds.clear()
                    break
                buffer.extend(chunk)
    finally:
        sel.close()
    return bytes(out), bytes(err), timed_out, overflow


def _identity_verified(pid: int, proc: subprocess.Popen, bound_starttime: int | None) -> bool:
    """True only with a recorded int identity that still matches live state."""
    if type(pid) is not int or pid <= 1:
        return False
    try:
        if proc.pid != pid:
            return False
    except Exception:
        return False
    if type(bound_starttime) is not int:
        return False
    now = proc_starttime(pid)
    return now is not None and now == bound_starttime


def terminate_only(
    pid: int, proc: subprocess.Popen, grace_s: float, bound_starttime: int | None = None
) -> bool:
    """SIGTERM exactly the bound pid, then SIGKILL only if it survives.

    Fails closed: a missing, bool or mismatched recorded identity never
    signals on the bare PID.  Returns True when the child was confirmed
    reaped (poll is not None), False otherwise.  Never signals a group.
    """
    grace = grace_s if type(grace_s) in (int, float) and grace_s > 0 else _TEARDOWN_GRACE_S
    if proc.poll() is not None:
        return True
    if not _identity_verified(pid, proc, bound_starttime):
        return False
    try:
        proc.terminate()
    except (ProcessLookupError, PermissionError):
        return False
    try:
        proc.wait(timeout=float(grace))
    except subprocess.TimeoutExpired:
        kill_only(pid, proc, bound_starttime=bound_starttime)
    return proc.poll() is not None


def kill_only(pid: int, proc: subprocess.Popen, bound_starttime: int | None = None) -> bool:
    """SIGKILL exactly the bound pid as a final step.

    Fails closed without a verified recorded identity.  Returns True
    when the child was confirmed reaped, False otherwise.
    """
    if proc.poll() is not None:
        return True
    if not _identity_verified(pid, proc, bound_starttime):
        return False
    try:
        proc.kill()
    except (ProcessLookupError, PermissionError):
        return False
    try:
        proc.wait(timeout=_TEARDOWN_WAIT_S)
    except Exception:
        pass
    return proc.poll() is not None


def safe_kill(pid: int, starttime: int | None = None) -> bool:
    """Kill one recorded pid only with a verified spawn-time identity.

    Requires a plain-int recorded *starttime* that still matches the
    live /proc starttime.  A bare pid, None, bool or mismatched
    identity never signals.  Returns True when SIGKILL was delivered.
    """
    if type(pid) is not int or pid <= 1:
        return False
    if type(starttime) is not int:
        return False
    now = proc_starttime(pid)
    if now is None or now != starttime:
        return False
    try:
        os.kill(pid, 9)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def close_pipes(proc: subprocess.Popen) -> None:
    """Close stdout/stderr pipe handles after collection."""
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def create_run_scratch(cwd: str | Path | None) -> tuple[Path, bool]:
    """Create or adopt the run scratch directory.

    Returns ``(workdir, created)`` where *created* is True only for a
    host-owned ``mkdtemp`` directory the caller must remove and whose
    removal is observed.
    """
    if cwd is None:
        workdir = Path(tempfile.mkdtemp(prefix="rea-runtime-"))
        return workdir, True
    workdir = Path(cwd)
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir, False


def destroy_run_scratch(workdir: Path, created: bool) -> tuple[bool, BaseException | None]:
    """Remove a host-owned scratch directory and observe its absence.

    Returns ``(removed, exc)``.  When *created* is False the caller
    keeps the directory and ``removed`` is True with no exception.
    Never claims removal that was not observed.
    """
    if not created:
        return True, None
    try:
        shutil.rmtree(workdir)
        removed = not workdir.exists() and not workdir.is_symlink()
        if not removed:
            return False, OSError(f"scratch was not removed: {workdir}")
        return True, None
    except BaseException as exc:
        return False, exc


def create_stage_dir() -> tuple[Path, bool]:
    """Create a host-owned launch staging directory outside scratch."""
    stage_dir = Path(tempfile.mkdtemp(prefix="rea-launch-"))
    return stage_dir, True


def destroy_stage_dir(stage_dir: Path | None, created: bool) -> bool:
    """Remove the launch snapshot and verify that the staging path is absent."""
    if stage_dir is None or not created:
        return True
    removed, error = destroy_run_scratch(stage_dir, created)
    if error is not None and not isinstance(error, Exception):
        raise error
    if isinstance(error, FileNotFoundError):
        return not stage_dir.exists() and not stage_dir.is_symlink()
    return removed
