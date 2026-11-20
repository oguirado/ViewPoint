# SPDX-License-Identifier: AGPL-3.0-only
"""Frame handoff with one active borrower and one waiting frame."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from threading import Lock

from .media import check_extent


@dataclass(frozen=True)
class FrameBlock:
    pixels: bytes
    index: int
    pts: int
    width: int
    height: int
    epoch: object
    end: bool
    pixel_format: str = "rgb24"
    consumer: str = "record"
    priority: int = 0
    deadline_ns: int | None = None
    affinity: tuple = ()

    @property
    def stride(self):
        return self.width * 3


class FrameQueue:
    """offer transfers release responsibility, including refused offers.

    borrow yields an epoch-checked reader. expire fences access, drains the
    waiting slot and lets the active borrow settle at its context boundary.
    """
    def __init__(self, mode=None, *, latest=False):
        self.mode = os.getenv("VIEWPOINT_TORO", "off") if mode is None else mode
        if self.mode not in {"off", "observe", "on"}:
            raise ValueError("invalid Toro mode")
        self.fallback_reason = "Toro backend unavailable" if self.mode == "on" else None
        self.epoch = object()
        self.latest = latest
        self._lock = Lock()
        self._waiting = None
        self._active = None
        self._open = True

    def _check(self, epoch):
        if not self._open or self.epoch is not epoch:
            raise ValueError("expired or foreign frame epoch")

    def offer(self, epoch, frame, release):
        if not callable(release):
            raise TypeError("release must be callable")
        retired = None
        try:
            with self._lock:
                self._check(epoch)
                if not isinstance(frame, FrameBlock) or frame.epoch is not epoch:
                    raise ValueError("foreign frame block epoch")
                check_extent(frame.width, frame.height, 1)
                if (frame.pixel_format != "rgb24" or type(frame.pixels) is not bytes
                        or len(frame.pixels) != frame.stride * frame.height):
                    raise ValueError("invalid RGB block extent")
                if self._waiting is not None and not self.latest:
                    raise BufferError("ordered frame queue full")
                retired, self._waiting = self._waiting, (frame, release)
        except BaseException:
            release()
            raise
        if retired is not None:
            retired[1]()

    @contextmanager
    def borrow(self, epoch):
        with self._lock:
            self._check(epoch)
            if self._active is not None or self._waiting is None:
                raise BufferError("frame unavailable")
            entry = self._waiting
            self._active, self._waiting = entry, None

        def read():
            with self._lock:
                self._check(epoch)
                if self._active is not entry:
                    raise ValueError("frame borrow ended")
                return entry[0]

        try:
            yield read
        finally:
            with self._lock:
                self._active = None
            entry[1]()

    def expire(self):
        with self._lock:
            self._open = False
            retired, self._waiting = self._waiting, None
        if retired is not None:
            retired[1]()


def open_frame_source(mode=None, *, latest=False, toro_open=None):
    """W7 can inject an admitted queue provider. Only open-time failure falls back."""
    normal = FrameQueue(mode, latest=latest)
    if normal.mode == "on" and toro_open is not None:
        try:
            backend = toro_open(latest=latest)
            if backend is not None:
                return backend
        except OSError as exc:
            normal.fallback_reason = f"Toro open failed: {exc}"
    return normal
