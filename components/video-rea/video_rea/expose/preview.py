# SPDX-License-Identifier: AGPL-3.0-only
"""Run trusted, application-registered Python preview functions on in-memory JSON.

The single-use thread lets Linux deny filesystem I/O for the callback without
changing the CLI, server, or caller's syscall permissions.
"""

from concurrent.futures import ThreadPoolExecutor
import ctypes as c
import errno
import mmap

from .wire import encode, loads


class _ArgComparison(c.Structure):
    _fields_ = [("arg", c.c_uint), ("op", c.c_uint),
                ("datum_a", c.c_uint64), ("datum_b", c.c_uint64)]


def _restrict_thread():
    lib = c.CDLL("libseccomp.so.2")
    lib.seccomp_init.argtypes = [c.c_uint32]
    lib.seccomp_init.restype = c.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [c.c_char_p]
    lib.seccomp_rule_add_array.argtypes = [
        c.c_void_p, c.c_uint32, c.c_int, c.c_uint, c.POINTER(_ArgComparison),
    ]
    lib.seccomp_load.argtypes = [c.c_void_p]
    lib.seccomp_release.argtypes = [c.c_void_p]
    lib.seccomp_release.restype = None
    allow = 0x7fff0000  # SECCOMP_RET_ALLOW
    context = lib.seccomp_init(0x00050000 | errno.EPERM)  # SECCOMP_RET_ERRNO
    if not context:
        raise RuntimeError("preview isolation unavailable")

    def permit(name, condition=None):
        number = lib.seccomp_syscall_resolve_name(name.encode())
        comparison = c.byref(condition) if condition is not None else None
        if number < 0 or lib.seccomp_rule_add_array(context, allow, number, int(condition is not None), comparison):
            raise RuntimeError("cannot install preview syscall rule")

    try:
        for name in ("brk", "munmap", "mremap", "madvise", "futex", "gettid",
                     "getpid", "clock_gettime", "rt_sigprocmask", "rt_sigreturn", "exit"):
            permit(name)
        # Allow Python's anonymous private allocator mappings, including large results.
        permit("mmap", _ArgComparison(3, 4, mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS, 0))  # SCMP_CMP_EQ
        if lib.seccomp_load(context):
            raise RuntimeError("cannot isolate preview thread")
    finally:
        lib.seccomp_release(context)


def run_preview(fn, args):
    def compute():
        _restrict_thread()
        return encode(fn(args))

    # Never reuse this thread or deserialize provider-defined Python objects.
    with ThreadPoolExecutor(max_workers=1) as worker:
        return loads(worker.submit(compute).result())

