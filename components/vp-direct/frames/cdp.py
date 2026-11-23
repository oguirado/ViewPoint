"""Read a raster and its accessibility witness from one attached CDP session."""

import base64
import time
from hashlib import sha256

from .source import FrameUnavailable, Raster
from schemas.validation import canonical_bytes


class CDPRaster:
    """Read-only capture port for an existing flattened page session."""

    def __init__(self, send, session_id, *, clock=time.monotonic_ns):
        if not isinstance(session_id, str) or not session_id:
            raise FrameUnavailable("missing CDP session identity")
        self._send = send
        self._session_id = session_id
        self._clock = clock

    def _call(self, method, params=None):
        return self._send(method, params, session_id=self._session_id)

    def _document(self, target_id):
        target = self._call("Target.getTargetInfo")["targetInfo"]
        if target.get("targetId") != target_id or target.get("type") != "page":
            raise FrameUnavailable("CDP session target changed")
        root = self._call("Page.getFrameTree")["frameTree"]["frame"]
        identity = (root.get("id"), root.get("loaderId"))
        if not all(isinstance(value, str) and value for value in identity):
            raise FrameUnavailable("CDP document identity unavailable")
        return identity

    def __call__(self, state):
        if state.backend != "cdp" or not state.cdp_target_id:
            raise FrameUnavailable("missing CDP target binding")
        try:
            document = self._document(state.cdp_target_id)
            captured = self._clock()
            reply = self._call("Page.captureScreenshot", {"format": "png"})
            png = base64.b64decode(reply["data"], validate=True)
            nodes = self._call("Accessibility.getFullAXTree", {"frameId": document[0]})["nodes"]
            if not isinstance(nodes, list) or not nodes or not all(
                isinstance(node, dict) and node.get("nodeId") for node in nodes
            ):
                raise FrameUnavailable("CDP AX witness unavailable")
            digest = sha256(canonical_bytes(nodes)).hexdigest()
            if self._document(state.cdp_target_id) != document:
                raise FrameUnavailable("CDP document changed during capture")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise FrameUnavailable("invalid CDP capture evidence") from exc
        return Raster(state, "cdp_png", png, captured, digest)
