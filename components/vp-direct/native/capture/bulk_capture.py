# SPDX-License-Identifier: AGPL-3.0-only
"""Process-local RGB24 capture stage using the pinned VP bulk v1 ABI."""

import ctypes as ct
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import mmap
import os
import sys
import threading
from time import monotonic_ns

from capture import Capture, FrameInfo

_ABI = 1
_OK = 0
_INVALID, _DEADLINE, _BROKEN, _ABI_MISMATCH = -1, -2, -3, -6
_FRAME, _ORDERED = 2, 1
_HAS_PRIORITY, _HAS_EXPECTED_ORDER = 1, 2
_REQUIRED_SEALS = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW
_U64 = ct.c_uint64
_U32 = ct.c_uint32
_I32 = ct.c_int32
_PTR = ct.c_void_p


class BulkError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"VP bulk status {status}")


class _Intent(ct.Structure):
    _fields_ = [
        ("consumer_id", _U64), ("deadline_monotonic_ns", _U64),
        ("affinity_group", _U64), ("expected_order", _U64),
        ("priority", _I32), ("flags", _U32),
    ]


class _Lease(ct.Structure):
    _fields_ = [
        ("session_id", _U64), ("lane_id", _U64), ("generation", _U64),
        ("reference", _U64), ("epoch", _U64),
    ]


class _Block(ct.Structure):
    _fields_ = [
        ("lease", _Lease), ("offset", _U64), ("length", _U64), ("skipped", _U64),
        ("kind", _U32), ("width", _U32), ("height", _U32), ("row_stride", _U32),
        ("format_id", _U32), ("fd", _I32),
    ]


class _View(ct.Structure):
    _fields_ = [("data", _PTR), ("length", _U64)]


class _Api(ct.Structure):
    _fields_ = [
        ("abi_version", _U32), ("struct_size", _U32), ("context", _PTR),
        ("publish_batch", ct.CFUNCTYPE(_I32, _PTR, _U64, ct.POINTER(_Intent),
                                      ct.POINTER(_Block), _U32, ct.POINTER(_U32))),
        ("borrow_batch", ct.CFUNCTYPE(_I32, _PTR, _U64, ct.POINTER(_Intent), _U32,
                                     ct.POINTER(_Block), _U32, ct.POINTER(_U32))),
        ("cpu_view", ct.CFUNCTYPE(_I32, _PTR, ct.POINTER(_Lease), ct.POINTER(_View))),
        ("release", ct.CFUNCTYPE(_I32, _PTR, ct.POINTER(_Lease))),
        ("break_and_drain", ct.CFUNCTYPE(_I32, _PTR, _U64)),
    ]


def _negotiate(lib):
    try:
        bootstrap = lib.vp_bulk_get_api_v1
    except AttributeError:
        raise BulkError(_ABI_MISMATCH) from None
    bootstrap.argtypes = [_U32] * 6 + [ct.POINTER(_Api)]
    bootstrap.restype = _I32
    api = _Api()
    sizes = tuple(ct.sizeof(layout) for layout in (_Api, _Intent, _Lease, _Block, _View))
    if bootstrap(_ABI, *sizes, ct.byref(api)) != _OK:
        raise BulkError(_ABI_MISMATCH)
    if (api.abi_version != _ABI or api.struct_size != sizes[0] or not api.context
            or not all(getattr(api, name) for name, _ in _Api._fields_[3:])):
        raise BulkError(_ABI_MISMATCH)
    return api


_PROVIDER_OWNER = os.getpid()
_PROVIDER_LOCK = threading.Lock()
_PROVIDERS = {}


class _Provider:
    """Keep the verified image and callback table live throughout custody."""

    def __init__(self, library, expected_sha256):
        if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64
                or any(digit not in "0123456789abcdef" for digit in expected_sha256)):
            raise BulkError(_ABI_MISMATCH)
        # Load a sealed snapshot of the exact hashed bytes. Keep its descriptor
        # open for the lifetime of the loaded image so its loader path stays unique.
        image = os.memfd_create("vp-bulk-provider", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        self.image = os.fdopen(image, "wb", buffering=0)
        try:
            digest = hashlib.sha256()
            with open(library, "rb") as original:
                while chunk := original.read(1 << 20):
                    digest.update(chunk)
                    if self.image.write(chunk) != len(chunk):
                        raise BulkError(_ABI_MISMATCH)
            if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
                raise BulkError(_ABI_MISMATCH)
            fcntl.fcntl(image, fcntl.F_ADD_SEALS, _REQUIRED_SEALS | fcntl.F_SEAL_SEAL)
            self.lib = ct.CDLL(f"/proc/self/fd/{image}")
            self.api = _negotiate(self.lib)
        except BaseException:
            # dlopen may cache a successfully opened path even if validation
            # subsequently fails. Reserve that path for the process lifetime.
            if hasattr(self, "lib"):
                _PROVIDERS[expected_sha256] = self
            else:
                self.image.close()
            raise


def _load_provider(library, expected_sha256):
    if os.getpid() != _PROVIDER_OWNER:
        raise BulkError(_BROKEN)
    with _PROVIDER_LOCK:
        cached = _PROVIDERS.get(expected_sha256) if isinstance(expected_sha256, str) else None
        if cached is not None and not hasattr(cached, "api"):
            raise BulkError(_ABI_MISMATCH)
        if cached is None:
            cached = _Provider(library, expected_sha256)
            _PROVIDERS[expected_sha256] = cached
        return cached


@contextmanager
def _stage_memfd(capture, length):
    fd = os.memfd_create("vp-staged-rgb24", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        # Stage in anonymous memory. The memfd is first mapped by the provider
        # after sealing and extent validation, with no writable alias.
        with mmap.mmap(-1, length, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS) as pixels:
            digest = capture.next_frame(pixels)
            with memoryview(pixels) as scratch:
                written = 0
                while written < length:
                    amount = os.write(fd, scratch[written:])
                    if not amount:
                        raise BulkError(_BROKEN)
                    written += amount
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, _REQUIRED_SEALS)
        yield fd, digest
    finally:
        os.close(fd)


@dataclass(frozen=True)
class VerifiedFrame:
    info: FrameInfo
    sha256: bytes


def _digest_view(view, expected_length):
    if (view.length != expected_length or view.length > sys.maxsize
            or (view.length != 0 and (not view.data or view.data > 2 * sys.maxsize + 1 - view.length))):
        raise BulkError(_INVALID)
    if not view.length:
        return hashlib.sha256(b"").digest()
    # No exported object or caller callback can outlive the native lease.
    pixels = (ct.c_ubyte * view.length).from_address(view.data)
    with memoryview(pixels).cast("B").toreadonly() as readonly:
        return hashlib.sha256(readonly).digest()


class CaptureStage:
    """The trusted, in-process capture stage. Pixels never leave this stage."""

    def __init__(self, display, server_pid, *, library, provider_sha256, session_id, lane_id,
                 consumer_id, affinity_group, generation, epoch):
        identities = (session_id, lane_id, consumer_id, affinity_group, generation, epoch)
        if any(type(value) is not int or not 0 < value < 2**64 for value in identities):
            raise BulkError(_INVALID)
        self._owner = os.getpid()
        self._lock = threading.Lock()
        self._provider = _load_provider(library, provider_sha256)
        self._api = self._provider.api
        self._base = _Lease(session_id, lane_id, generation, 0, epoch)
        self._intent_ids = (consumer_id, affinity_group)
        self._order = 0
        self._closed = False
        self._closing = False
        self._outstanding = None
        self._capture = Capture(display, server_pid, library=self._provider.lib)

    def _check_owner(self):
        if os.getpid() != self._owner:
            raise BulkError(_BROKEN)

    def capture_and_verify(self, *, deadline_monotonic_ns, priority):
        self._check_owner()
        if (type(deadline_monotonic_ns) is not int or not 0 < deadline_monotonic_ns < 2**64
                or type(priority) is not int or not -(2**31) <= priority < 2**31):
            raise BulkError(_INVALID)
        with self._lock:
            if self._closing:
                raise BulkError(_BROKEN)
            if monotonic_ns() >= deadline_monotonic_ns:
                raise BulkError(_DEADLINE)
            self._order += 1
            if self._order >= 2**64:
                self._close()
                raise BulkError(_BROKEN)
            consumer, group = self._intent_ids
            intent = _Intent(consumer, deadline_monotonic_ns, group, self._order, priority,
                             _HAS_PRIORITY | _HAS_EXPECTED_ORDER)
            try:
                return self._verify(intent)
            except BaseException:
                self._close()
                raise

    def _verify(self, intent):
        info = self._capture.info
        api = self._api
        base = self._base
        # The publisher closes its original on every result.
        with _stage_memfd(self._capture, info.byte_len) as (source, digest):
            published = _Block(_Lease(base.session_id, base.lane_id, base.generation,
                                      intent.expected_order, base.epoch),
                               0, info.byte_len, 0, _FRAME, info.width, info.height,
                               info.row_stride, info.format_id, source)
            count = _U32()
            status = api.publish_batch(api.context, base.lane_id, ct.byref(intent),
                                       ct.byref(published), 1, ct.byref(count))
        if status != _OK or count.value != 1:
            raise BulkError(status or _BROKEN)

        borrowed = _Block()
        count.value = 0
        try:
            status = api.borrow_batch(api.context, base.lane_id, ct.byref(intent), _ORDERED,
                                      ct.byref(borrowed), 1, ct.byref(count))
            if status != _OK or count.value != 1:
                raise BulkError(status or _BROKEN)
            expected = (base.session_id, base.lane_id, base.generation, base.epoch, _FRAME,
                        0, info.byte_len, 0, info.width, info.height, info.row_stride, info.format_id)
            lease = borrowed.lease
            delivered = (lease.session_id, lease.lane_id, lease.generation, lease.epoch, borrowed.kind,
                         borrowed.offset, borrowed.length, borrowed.skipped, borrowed.width,
                         borrowed.height, borrowed.row_stride, borrowed.format_id)
            if delivered != expected or borrowed.fd < 0:
                raise BulkError(_BROKEN)
            view = _View()
            status = api.cpu_view(api.context, ct.byref(lease), ct.byref(view))
            if status != _OK:
                raise BulkError(status)
            if not hmac.compare_digest(_digest_view(view, info.byte_len), digest):
                raise BulkError(_BROKEN)
            return VerifiedFrame(info, digest)
        finally:
            if count.value == 1:
                self._outstanding = borrowed.lease
                self._release_loan()

    def _release_loan(self):
        status = self._api.release(self._api.context, ct.byref(self._outstanding))
        if status != _OK:
            raise BulkError(status)
        self._outstanding = None

    def _close(self):
        if self._closed:
            return
        self._closing = True
        try:
            status = self._api.break_and_drain(self._api.context, self._base.session_id)
            if self._outstanding is not None:
                self._release_loan()
                status = self._api.break_and_drain(self._api.context, self._base.session_id)
        finally:
            self._capture.close()
        if status != _OK:
            raise BulkError(status)
        self._closed = True

    def close(self):
        self._check_owner()
        with self._lock:
            self._close()

    def __enter__(self):
        self._check_owner()
        return self

    def __exit__(self, *_exception):
        self.close()
