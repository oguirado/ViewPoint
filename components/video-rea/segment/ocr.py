# SPDX-License-Identifier: AGPL-3.0-only
"""Pinned, deadline-bounded OCR and integer crop preparation."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import selectors
import struct
import subprocess
import sys
from time import monotonic, perf_counter_ns

import cv2
import numpy as np

from source.proc import ChildError, require_namespace
from source.records import digest

from ._ocr_worker import normalize
from .temporal import luma


TESSDATA = Path.home() / ".local" / "share" / "tessdata"
ENG_SHA256 = "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"
STARTUP_SECONDS = 3
READ_SECONDS = 0.4
MAX_REPLY = 16 << 10
MAX_IMAGE = 4 << 20


class OCRProcess:
    """Own and reap one exact OCR child, including on partial I/O and deadline."""
    def __init__(self, path):
        require_namespace()
        env = dict(os.environ, OMP_THREAD_LIMIT="1", OMP_NUM_THREADS="1",
                   OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
        self.child = subprocess.Popen(
            [sys.executable, "-B", "-m", "segment._ocr_worker", str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
        )
        try:
            os.set_blocking(self.child.stdin.fileno(), False)
            os.set_blocking(self.child.stdout.fileno(), False)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait()
        self.child.stdin.close()
        self.child.stdout.close()

    @staticmethod
    def transfer(stream, deadline, event, payload):
        remaining = len(payload) if event == selectors.EVENT_WRITE else payload
        offset = 0
        chunks = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(stream, event)
            while remaining:
                seconds = deadline - monotonic()
                if seconds <= 0 or not selector.select(seconds):
                    raise TimeoutError("OCR deadline")
                if event == selectors.EVENT_WRITE:
                    count = os.write(stream.fileno(), payload[offset:])
                else:
                    data = os.read(stream.fileno(), remaining)
                    count = len(data)
                    chunks.extend(data)
                if not count:
                    raise EOFError("OCR pipe closed")
                remaining -= count
                offset += count
        return bytes(chunks)

    def exchange(self, seconds, image=None):
        deadline = monotonic() + seconds
        if image is not None:
            h, w = image.shape
            self.transfer(self.child.stdin, deadline, selectors.EVENT_WRITE,
                          struct.pack("!II", w, h) + image.tobytes())
        pipe = self.child.stdout
        size, = struct.unpack("!I", self.transfer(pipe, deadline, selectors.EVENT_READ, 4))
        if not 0 < size <= MAX_REPLY:
            raise ValueError("OCR response bounds")
        return json.loads(self.transfer(pipe, deadline, selectors.EVENT_READ, size))


def prepare(rgb, row):
    x, y, w, h = row.ink
    pad = 2
    x0, y0 = max(0, x-pad), max(0, y-pad)
    gray = luma(rgb[y0:y+h+pad, x0:x+w+pad])
    if row.light_ink:
        gray = 255 - gray
    histogram = np.cumsum(np.bincount(gray.ravel(), minlength=256))
    low, high = (int(np.searchsorted(histogram, max(1, gray.size*p//100))) for p in (6, 89))
    if high - low < 12:
        return None
    scaled = np.uint8(np.clip((gray.astype(np.int32) - low) * 255 // (high-low), 0, 255))
    scale = max(1, (42+h//2)//h)
    image = cv2.resize(scaled, (scaled.shape[1]*scale, scaled.shape[0]*scale),
                       interpolation=cv2.INTER_LINEAR_EXACT)
    return cv2.copyMakeBorder(image, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)


class OCR:
    def __init__(self, directory):
        self.directory = directory
        self.api = None
        self.reason = "tessdata_absent"
        self.calls = self.hits = self.total_ns = self.timeouts = 0
        self.warm_ns = []
        self.cache = {}
        self.cold_ns = None
        self.engine = None
        try:
            if digest((directory / "eng.traineddata").read_bytes()) != ENG_SHA256:
                self.reason = "tessdata_pin_mismatch"
                return
        except OSError:
            return
        self.start()

    def start(self):
        started = perf_counter_ns()
        try:
            self.api = OCRProcess(self.directory)
            self.reason = self.api.exchange(STARTUP_SECONDS)
        except TimeoutError:
            self.reason = "ocr_startup_timeout"
        except (ChildError, OSError, ValueError, EOFError):
            self.reason = "ocr_worker_unavailable"
        if self.reason:
            self.close()
        else:
            self.engine = "libtesseract"
            self.cold_ns = perf_counter_ns() - started

    def close(self):
        if self.api:
            self.api.close()
            self.api = None

    def read(self, rgb, row):
        if self.reason or row.ink is None:
            return None, self.reason or "no_visible_ink"
        image = prepare(rgb, row)
        if image is None:
            return None, "ocr_no_contrast"
        if image.size > MAX_IMAGE:
            return None, "ocr_crop_limit"
        key = (image.shape, image.tobytes())
        if key in self.cache:
            self.hits += 1
            return self.cache[key]
        if self.api is None:
            self.start()
            if self.reason:
                return None, self.reason
        started = perf_counter_ns()
        try:
            result = tuple(self.api.exchange(READ_SECONDS, image))
        except TimeoutError:
            self.timeouts += 1
            result = (None, "ocr_timeout")
            self.close()
        except (OSError, ValueError, EOFError):
            result = (None, "ocr_worker_failed")
            self.close()
        elapsed = perf_counter_ns() - started
        self.calls += 1
        self.total_ns += elapsed
        self.warm_ns.append(elapsed)
        if self.api and len(self.cache) < 512:
            self.cache[key] = result
        return result


@contextmanager
def open_ocr(path):
    engine = OCR(Path(path))
    try:
        yield engine
    finally:
        engine.close()
