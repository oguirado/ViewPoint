"""Owned bounded subprocesses. This is lifecycle control, not a security sandbox.

Only reviewed local CAD scripts are accepted. No script is executed by a
production call before licence, pin and resource admissions have passed.
"""
from asset_factory import proc as af_proc
import ctypes
import errno
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from tools.cad.contracts import Refusal, require


def publish_new_directory(staged, target):
    """Linux RENAME_NOREPLACE: publication never replaces caller bytes."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    require(rename is not None, 3, 'atomic_noreplace_unavailable')
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staged), -100, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        raise Refusal(5 if error in (errno.EEXIST, errno.ENOTEMPTY) else 2,
                      'output_exists' if error in (errno.EEXIST, errno.ENOTEMPTY) else 'publication_failed')


def run(argv, *, cwd, timeout_s, env):
    require(0 < timeout_s <= 300, 6, 'deadline_exceeded')
    start = time.monotonic_ns()
    with tempfile.TemporaryFile() as out:
        process = af_proc.popen(argv, cwd=cwd, env=env, stdout=out,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            af_proc.wait_exit(process, timeout=timeout_s)
        except BaseException as exc:
            timed_out = isinstance(exc, subprocess.TimeoutExpired)
            try:
                af_proc.signal_group(process, signal.SIGKILL)
            except ProcessLookupError:
                pass
            af_proc.wait_exit(process, timeout=5)
            if not timed_out:
                raise
        finally:
            # Also reap descendants that outlive a successful parent.
            try:
                af_proc.signal_group(process, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        size = os.fstat(out.fileno()).st_size
        out.seek(max(0, size-4096))
        tail = out.read().decode('utf-8', errors='replace')
    # Child exit 1 and signal exits are never exposed as a local stage code.
    stage_rc = process.returncode if process.returncode in (0, 2, 3, 4, 5, 6) else 2
    return {'rc': 6 if timed_out else stage_rc, 'exit_code': process.returncode,
            'timed_out': timed_out, 'wall_ms': (time.monotonic_ns()-start)//10**6,
            'output_bytes': size, 'tail': tail}


def environment(home, pythonpath):
    """An explicit environment with no inherited credentials or model settings.

    Offline is a caller precondition, not an isolation claim. No worker in this
    unit opens network connections or attempts package installation.
    """
    return {'PATH': '/usr/bin:/bin', 'HOME': str(Path(home)), 'LC_ALL': 'C',
            'LANG': 'C', 'TZ': 'UTC', 'PYTHONHASHSEED': '0',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(pythonpath),
            'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}
