# @module {"id":"wa.learn.chrome_driver","layer":2,"owner":"LEARN","deps":["wa.perceive.cdp_pipe","wa.perceive.browser","wa.act.cdp_executor","wa.perceive.vision_adapter","wa.learn.explore"],"book":"HB-WA-s4-learn"}
"""@module wa.learn.chrome_driver - Chromium Driver adapter implementing Driver for web exploration.

Provides ChromeDriver: observes browser STATE via page_state and RAM screenshots, lists actionable
link/button candidate actions with irreversible classification, executes actions via cdp_executor,
and handles reset navigation.
"""
import base64
import struct
import zlib

from ..act import cdp_executor
from ..perceive import ax, browser, vision_adapter

try:
    from .explore import Driver
except ImportError:
    class Driver:
        """Abstract base class fallback when explore.py is absent."""
        pass


def _png_to_frame(png_bytes):
    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Invalid PNG signature")
    offset = 8
    idat = []
    width = height = bit_depth = color_type = 0
    while offset < len(png_bytes):
        length, chunk_type = struct.unpack(">I4s", png_bytes[offset:offset + 8])
        offset += 8
        data = png_bytes[offset:offset + length]
        offset += length + 4
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_m, interlace = struct.unpack(">IIBBBBB", data)
        elif chunk_type == b"IDAT":
            idat.append(data)
        elif chunk_type == b"IEND":
            break
    raw = zlib.decompress(b"".join(idat))
    bpp = 4 if color_type == 6 else 3
    stride = width * bpp
    recon = bytearray()
    prev_row = bytearray(stride)
    idx = 0

    def paeth(a, b, c):
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        if pa <= pb and pa <= pc:
            return a
        elif pb <= pc:
            return b
        return c

    for y in range(height):
        filter_type = raw[idx]
        idx += 1
        row = bytearray(stride)
        for x in range(stride):
            filt = raw[idx]
            idx += 1
            left = row[x - bpp] if x >= bpp else 0
            up = prev_row[x]
            up_left = prev_row[x - bpp] if x >= bpp else 0
            if filter_type == 0:
                val = filt
            elif filter_type == 1:
                val = (filt + left) & 0xFF
            elif filter_type == 2:
                val = (filt + up) & 0xFF
            elif filter_type == 3:
                val = (filt + (left + up) // 2) & 0xFF
            elif filter_type == 4:
                val = (filt + paeth(left, up, up_left)) & 0xFF
            else:
                raise ValueError("Unknown PNG filter %d" % filter_type)
            row[x] = val
        prev_row = row
        if color_type == 6:
            for i in range(0, stride, 4):
                recon.extend(row[i:i + 3])
        else:
            recon.extend(row)
    return vision_adapter.Frame(width, height, bytes(recon))


# @anchor:wa.learn.chrome_driver.ChromeDriver
class ChromeDriver(Driver):
    """Driver adapter wrapping CDP session and browser page state."""

    def __init__(self, cdp, session_id, start_url, vision=None):
        self.cdp = cdp
        self.session_id = session_id
        self.start_url = start_url
        self.vision = vision

    def observe(self):
        state = browser.page_state(self.cdp, self.session_id)
        screen_fp = None
        if isinstance(self.vision, vision_adapter.VisionEngineAdapter):
            res = self.cdp.send("Page.captureScreenshot", session_id=self.session_id)
            png_bytes = base64.b64decode(res["data"])
            frame = _png_to_frame(png_bytes)
            screen_fp = self.vision.screen_identity(frame)
        return state, screen_fp

    def candidate_actions(self, state):
        actions = []
        nodes = state.get("nodes", []) if isinstance(state, dict) else []
        for node in nodes:
            if not node.get("actionable"):
                continue
            role = node.get("role")
            if role in ax.ACTIONABLE_ROLES:
                name = (node.get("name") or "").lower()
                irreversible = any(kw in name for kw in ("delete", "remove", "pay"))
                actions.append({
                    "op": "click",
                    "target": node["id"],
                    "irreversible": irreversible,
                })
        return actions

    def act(self, action):
        # The executor needs the pruned AX nodes (they carry backend_id); STATE nodes deliberately do not.
        def read_nodes():
            raw = ax.raw_tree(self.cdp, self.session_id)
            try:
                return ax.prune(raw)
            finally:
                ax.release(self.cdp, self.session_id)

        nodes = read_nodes()

        receipt = cdp_executor.run_ax_action(
            self.cdp, self.session_id, nodes, action, read_nodes, retries=2
        )
        if not receipt.get("ok"):
            raise cdp_executor.ActionError("Action failed: %s" % receipt.get("error"))

    def reset(self):
        if self.cdp and self.session_id and self.start_url:
            self.cdp.navigate(self.session_id, self.start_url)
