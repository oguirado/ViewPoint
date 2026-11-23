"""ctypes control plane for the candidate VP capture worker."""

import ctypes as ct
from dataclasses import dataclass
from enum import IntEnum
import os
from pathlib import Path
import threading
import weakref


class Status(IntEnum):
    OK = 0
    INVALID_ARGUMENT = 1
    BAD_DISPLAY = 2
    UNSUPPORTED_FORMAT = 3
    BUFFER_TOO_SMALL = 4
    X11_ERROR = 5
    RESOURCE_ERROR = 6
    TIMED_OUT = 7
    CLOSED = 8


class CaptureError(RuntimeError):
    def __init__(self, status):
        self.status = Status(status)
        super().__init__(self.status.name)


class _FrameInfo(ct.Structure):
    _fields_ = [
        ("width", ct.c_uint32),
        ("height", ct.c_uint32),
        ("row_stride", ct.c_uint32),
        ("format_id", ct.c_uint32),
        ("byte_len", ct.c_uint64),
    ]


@dataclass(frozen=True)
class FrameInfo:
    width: int
    height: int
    row_stride: int
    format_id: int
    byte_len: int


def _load(library):
    if library is None:
        library = Path(__file__).with_name("target") / "release" / "libvp_capture.so"
    lib = library if isinstance(library, ct.CDLL) else ct.CDLL(str(library))
    handle = ct.POINTER(ct.c_void_p)
    lib.vp_capture_start.argtypes = [ct.c_uint32, ct.c_int32, handle, ct.POINTER(_FrameInfo)]
    lib.vp_capture_next_frame.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_size_t, ct.c_void_p]
    lib.vp_capture_stop.argtypes = [handle]
    for operation in (lib.vp_capture_start, lib.vp_capture_next_frame, lib.vp_capture_stop):
        operation.restype = ct.c_int32
    return lib


def _stop(owner_pid, lib, handle):
    # A post-fork child has no copy of the worker thread to join.
    if os.getpid() == owner_pid:
        return lib.vp_capture_stop(ct.byref(handle))
    return Status.CLOSED


class Capture:
    """Own a worker for one caller-owned local Xvfb. Close it before forking."""

    def __init__(self, display, server_pid, *, library=None):
        if (type(display) is not int or not 90 <= display <= 65535
                or type(server_pid) is not int or not 0 < server_pid <= 2**31 - 1):
            raise CaptureError(Status.BAD_DISPLAY)
        self._lib = _load(library)
        self._handle = ct.c_void_p()
        self._owner_pid = os.getpid()
        self._lock = threading.Lock()
        self._digest = (ct.c_uint8 * 32)()
        info = _FrameInfo()
        status = self._lib.vp_capture_start(display, server_pid, ct.byref(self._handle), ct.byref(info))
        if status != Status.OK:
            raise CaptureError(status)
        try:
            self._finalize = weakref.finalize(self, _stop, self._owner_pid, self._lib, self._handle)
            self.info = FrameInfo(info.width, info.height, info.row_stride, info.format_id, info.byte_len)
        except BaseException:
            _stop(self._owner_pid, self._lib, self._handle)
            raise

    def next_frame(self, buffer):
        """Fill an exclusively held, writable contiguous buffer and return its SHA-256."""
        if os.getpid() != self._owner_pid:
            raise CaptureError(Status.CLOSED)
        with self._lock:
            if not self._handle:
                raise CaptureError(Status.CLOSED)
            with memoryview(buffer) as view:
                if view.readonly or not view.c_contiguous:
                    raise ValueError("frame destination must be writable and C-contiguous")
                if view.nbytes < self.info.byte_len:
                    raise CaptureError(Status.BUFFER_TOO_SMALL)
                # ctypes keeps the export and its backing object alive through the call.
                destination = (ct.c_uint8 * view.nbytes).from_buffer(view)
                try:
                    status = self._lib.vp_capture_next_frame(
                        self._handle, destination, view.nbytes, self._digest
                    )
                finally:
                    del destination
            if status != Status.OK:
                raise CaptureError(status)
            return bytes(self._digest)

    def close(self):
        if os.getpid() != self._owner_pid:
            raise CaptureError(Status.CLOSED)
        with self._lock:
            status = self._finalize()
            if status not in (None, Status.OK):
                raise CaptureError(status)

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()
