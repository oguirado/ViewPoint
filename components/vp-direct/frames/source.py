"""S5 frame acquisition with an injected host binding and demand coordinator."""

import io
import json
import re
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock
from uuid import uuid4

from PIL import Image

from schemas.validation import validate_contract


class FrameUnavailable(RuntimeError):
    """A frame could not be acquired and verified for the current binding."""


@dataclass(frozen=True)
class HostFrameState:
    surface_id: str
    incarnation: str
    authority_epoch: int
    geometry_epoch: int
    window_witness: str
    window_rect: tuple[int, int, int, int]
    raster_size: tuple[int, int]
    display: str
    hidden: bool
    backend: str
    cdp_target_id: str | None = None


@dataclass(frozen=True)
class Raster:
    state: HostFrameState
    kind: str
    png: bytes
    captured_mono_ns: int
    ax_sha256: str


def inspect_png(data, expected_size):
    """Check decoded pixels and return their digest for visual comparisons."""
    if not isinstance(data, bytes):
        raise FrameUnavailable("raster must be immutable PNG bytes")
    try:
        with Image.open(io.BytesIO(data), formats=("PNG",)) as image:
            if image.size != expected_size or getattr(image, "is_animated", False):
                raise FrameUnavailable("raster size or format does not match the binding")
            image.verify()
        with Image.open(io.BytesIO(data), formats=("PNG",)) as image:
            pixels = image.convert("RGBA")
            # Ignore RGB values that would be invisible through transparency.
            visible = Image.new("RGBA", pixels.size, (0, 0, 0, 0))
            visible.alpha_composite(pixels)
            if all(low == high for low, high in visible.getextrema()):
                raise FrameUnavailable("blank raster")
            return sha256(visible.tobytes()).hexdigest()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise FrameUnavailable("invalid PNG raster") from exc


def _qualified_hidden_cdp(path):
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt["schema"] != "vp.l0_receipt/v1":
        raise FrameUnavailable("unknown W-L0 receipt schema")
    step = receipt["steps"]["L0-4"]
    return (
        step.get("status") == "PASS"
        and step.get("backend") == "cdp"
        and step.get("capture_method") == "Page.captureScreenshot"
        and step.get("hidden") is True
        and step.get("non_blank") is True
        and step.get("frames_differ") is True
        and step.get("storage") == "RAM"
    )


class FrameSource:
    """Acquire under a demand registration and retain verified rasters in RAM.

    current(surface_id) supplies authoritative snapshots of qualified bindings.
    It must advance epochs when authority or geometry is invalidated. The acquisition port
    returns Raster with the provenance it observed. register(demand) must return
    a context manager that holds an admitted coordinator registration until exit.
    """

    def __init__(self, current, backends, register, *, clock=time.monotonic_ns,
                 receipt=Path(__file__).resolve().parents[1] / "config" / "wl0.json"):
        self._current = current
        self._backends = dict(backends)
        self._register = register
        self._clock = clock
        self._hidden_cdp = _qualified_hidden_cdp(receipt)
        self._frames = {}
        self._lock = Lock()

    def capture(self, demand):
        demand = dict(demand)
        validate_contract("vp.demand/v1", demand)
        surface_id = demand["surface_id"]
        state = self._current(surface_id)
        if (
            not isinstance(state, HostFrameState)
            or state.surface_id != surface_id
            or not isinstance(state.window_witness, str)
            or not state.window_witness
            or not isinstance(state.incarnation, str)
            or not state.incarnation
            or type(state.authority_epoch) is not int
            or state.authority_epoch < 0
            or type(state.geometry_epoch) is not int
            or state.geometry_epoch < 0
            or not isinstance(state.window_rect, tuple)
            or len(state.window_rect) != 4
            or any(type(value) is not int for value in state.window_rect)
            or min(state.window_rect[2:]) < 1
            or not isinstance(state.raster_size, tuple)
            or len(state.raster_size) != 2
            or any(type(value) is not int or value < 1 for value in state.raster_size)
            or type(state.hidden) is not bool
            or not isinstance(state.backend, str)
            or not isinstance(state.display, str)
            or not re.fullmatch(r":0|xpra:[0-9]+", state.display)
        ):
            raise FrameUnavailable("missing or invalid host binding")

        if state.hidden and state.display == ":0" and (
            state.backend != "cdp" or not self._hidden_cdp
        ):
            raise FrameUnavailable("hidden host window needs qualified CDP")
        kind = {"cdp": "cdp_png", "xwd": "xwd", "xpra": "xpra"}.get(state.backend)
        acquire = self._backends.get(state.backend)
        if not kind or not callable(acquire) or (
            kind == "cdp_png" and (not isinstance(state.cdp_target_id, str) or not state.cdp_target_id)
        ):
            raise FrameUnavailable("no raster backend for the bound target")

        ram_ref = "ram:" + uuid4().hex
        published = False
        try:
            with self._register(demand):
                if self._current(surface_id) != state:
                    raise FrameUnavailable("binding changed before acquisition")
                started = self._clock()
                raster = acquire(state)
                ended = self._clock()
                if (
                    not isinstance(raster, Raster)
                    or raster.state != state
                    or raster.kind != kind
                    or not isinstance(raster.ax_sha256, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", raster.ax_sha256)
                ):
                    raise FrameUnavailable("raster provenance or AX witness mismatch")
                if type(raster.captured_mono_ns) is not int or not (
                    started <= raster.captured_mono_ns <= ended
                ):
                    raise FrameUnavailable("raster is outside this acquisition interval")
                inspect_png(raster.png, state.raster_size)
                witness = {"ax_sha256": raster.ax_sha256}
                if kind == "cdp_png":
                    witness["cdp_target_id"] = raster.state.cdp_target_id
                width, height = state.raster_size
                descriptor = {
                    "frame_kind": kind,
                    "frame_sha256": sha256(raster.png).hexdigest(),
                    "width": width,
                    "height": height,
                    "captured_mono_ns": raster.captured_mono_ns,
                    "witness": witness,
                    "ram_ref": ram_ref,
                }
                validate_contract("vp.frame/v1", descriptor)

                with self._lock:
                    if self._current(surface_id) != state:
                        raise FrameUnavailable("binding changed before publication")
                    self._frames[ram_ref] = raster
                    published = True
            if published and self._current(surface_id) != state:
                raise FrameUnavailable("binding changed before delivery")
        except BaseException:
            if published:
                self.release(ram_ref)
            raise
        if not published:
            raise FrameUnavailable("demand registration suppressed capture failure")
        return descriptor

    def read_ram(self, ram_ref):
        with self._lock:
            raster = self._frames.get(ram_ref)
            if raster is None or self._current(raster.state.surface_id) != raster.state:
                raise FrameUnavailable("retired or stale RAM reference")
            return raster.png

    def release(self, ram_ref):
        with self._lock:
            self._frames.pop(ram_ref, None)
