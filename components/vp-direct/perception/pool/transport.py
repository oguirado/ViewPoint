"""Use the existing VServe and C2 transports without implicit fallback."""

import json
from pathlib import Path
import sys

_COMPONENTS = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_COMPONENTS / "wender-agent"))

from wender.perceive.vision_adapter import Frame
from wender.perceive.vision_bridge import DescribeResult, SubprocessVisionEngine, _parse_description
from wender.perceive.vserve_client import VServeClient, VServeError


class VisionWorker:
    def __init__(self, root, *, timeout, transport, allow_fallback):
        self._vserve = (
            VServeClient(root, timeout=timeout, max_restarts=0)
            if transport == "vserve" else None
        )
        self._c2 = (
            SubprocessVisionEngine(root, timeout=timeout, persistent=False)
            if transport == "c2" or allow_fallback else None
        )
        self._closing_process = None

    def describe(self, transport, frame, previous, masks):
        if transport == "vserve":
            payload = self._vserve.describe(frame, previous, masks=masks)
            return _parse_description(json.dumps(payload, allow_nan=False).encode("utf-8"))
        return self._c2.describe(frame, previous, masks=masks)

    def close(self):
        if self._vserve is not None:
            # Keep the owned handle even when VServeClient clears its reference.
            if self._closing_process is None:
                self._closing_process = self._vserve._proc
            self._vserve.close()
            if self._closing_process is not None and self._closing_process.poll() is None:
                raise RuntimeError("owned VServe process has not exited")
            self._closing_process = None
        if self._c2 is not None:
            self._c2.close()
