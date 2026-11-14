#!/usr/bin/env python3
"""CDP client, pure AX tree / DOM snapshot query engine, and action planner."""

import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time


class RefusalError(Exception):
    """Raised when a method or frame is refused by local policy."""
    def __init__(self, message="refused", code=5):
        super().__init__(message)
        self.code = code


class OversizeFrameError(RefusalError):
    """Raised when an incoming or outgoing frame exceeds max_frame_bytes."""
    def __init__(self, message="oversize frame", code=5):
        super().__init__(message, code)


def load_allowlist(path=None):
    if path is None:
        path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "..",
                "..",
                "docker",
                "cu",
                "oracle",
                "allowlist.json",
            )
        )
    with open(path, "rb") as f:
        al = json.loads(f.read().decode("utf-8"))
    return al


REFUSAL_PATTERN = re.compile(
    r"(password|passwd|secret|token|cc-|card|cvv|cvc)", re.IGNORECASE
)


def _check_string_refusal(val):
    if not val or not isinstance(val, str):
        return False
    return bool(REFUSAL_PATTERN.search(val))


def is_refused_dom_node(node):
    attrs = node.get("attributes", {})
    node_type = str(attrs.get("type", "")).lower()
    if node_type in ("password", "file"):
        return True
    for key in ("id", "name"):
        if _check_string_refusal(attrs.get(key)):
            return True
    return False


def is_refused_ax_node(node):
    # AX node properties / name / role
    ax_name = node.get("name", "")
    if _check_string_refusal(ax_name):
        return True
    # check if properties contain type or values matching
    for prop in node.get("properties", []):
        pname = prop.get("name")
        pval = prop.get("value", {})
        val_str = str(pval.get("value") if isinstance(pval, dict) else pval)
        if pname == "type" and val_str.lower() in ("password", "file"):
            return True
        if pname in ("id", "name") and _check_string_refusal(val_str):
            return True
    return False


def parse_dom_snapshot(dom_data):
    if not dom_data or not isinstance(dom_data, dict):
        return []
    documents = dom_data.get("documents", [])
    if not documents:
        return []
    doc = documents[0]
    strings = dom_data.get("strings", [])
    scroll_x = doc.get("scrollOffsetX", 0)
    scroll_y = doc.get("scrollOffsetY", 0)

    nodes_data = doc.get("nodes", {})
    backend_ids = nodes_data.get("backendNodeId", [])
    raw_attrs_list = nodes_data.get("attributes", [])

    layout_data = doc.get("layout", {})
    node_indices = layout_data.get("nodeIndex", [])
    bounds_list = layout_data.get("bounds", [])
    styles_list = layout_data.get("styles", [])

    # Map nodeIndex -> layout_idx
    layout_by_node = {}
    for l_idx, n_idx in enumerate(node_indices):
        layout_by_node[n_idx] = l_idx

    parsed_nodes = []
    num_nodes = len(backend_ids)
    for n_idx in range(num_nodes):
        b_id = backend_ids[n_idx]
        raw_attrs = raw_attrs_list[n_idx] if n_idx < len(raw_attrs_list) else []
        attrs = {}
        for k in range(0, len(raw_attrs), 2):
            if k + 1 < len(raw_attrs):
                k_str = strings[raw_attrs[k]] if raw_attrs[k] < len(strings) else ""
                v_str = strings[raw_attrs[k + 1]] if raw_attrs[k + 1] < len(strings) else ""
                attrs[k_str] = v_str

        bounds = None
        is_visible = False
        center_doc = [0, 0]
        center_viewport = [0, 0]

        if n_idx in layout_by_node:
            l_idx = layout_by_node[n_idx]
            raw_bounds = bounds_list[l_idx] if l_idx < len(bounds_list) else [0, 0, 0, 0]
            x, y, w, h = [int(v) for v in raw_bounds]
            bounds = [x, y, w, h]

            styles = styles_list[l_idx] if l_idx < len(styles_list) else []
            # computedStyles requested: ["visibility", "opacity", "display"]
            vis = strings[styles[0]] if len(styles) > 0 and styles[0] < len(strings) else ""
            opacity_str = strings[styles[1]] if len(styles) > 1 and styles[1] < len(strings) else "1"
            disp = strings[styles[2]] if len(styles) > 2 and styles[2] < len(strings) else ""

            try:
                opacity_val = float(opacity_str)
            except ValueError:
                opacity_val = 1.0

            if vis == "visible" and opacity_val != 0.0 and disp != "none" and w > 0 and h > 0:
                is_visible = True

            cx = x + w // 2
            cy = y + h // 2
            center_doc = [cx, cy]
            center_viewport = [cx - int(scroll_x), cy - int(scroll_y)]

        parsed_nodes.append({
            "nodeIndex": n_idx,
            "backendNodeId": b_id,
            "attributes": attrs,
            "bounds": bounds,
            "is_visible": is_visible,
            "center_doc": center_doc,
            "center_viewport": center_viewport,
            "scrollOffsetX": scroll_x,
            "scrollOffsetY": scroll_y,
        })

    return parsed_nodes


def parse_ax_tree(ax_data):
    if not ax_data or not isinstance(ax_data, dict):
        return []
    raw_nodes = ax_data.get("nodes", [])
    parsed = []
    for node in raw_nodes:
        node_id = str(node.get("nodeId", ""))
        backend_id = node.get("backendDOMNodeId")

        # role parsing
        role_obj = node.get("role", {})
        if isinstance(role_obj, dict):
            role = role_obj.get("value", "")
        else:
            role = str(role_obj)

        # name parsing
        name_obj = node.get("name", {})
        if isinstance(name_obj, dict):
            name = name_obj.get("value", "")
        else:
            name = str(name_obj)

        properties = node.get("properties", [])
        is_focused = False
        for prop in properties:
            if prop.get("name") == "focused":
                p_val = prop.get("value", {})
                if isinstance(p_val, dict):
                    is_focused = bool(p_val.get("value"))
                else:
                    is_focused = bool(p_val)

        parsed.append({
            "nodeId": node_id,
            "backendDOMNodeId": backend_id,
            "role": role,
            "name": name,
            "focused": is_focused,
            "properties": properties,
            "raw": node,
        })
    return parsed


def find_node_by_id(dom_nodes, element_id):
    for node in dom_nodes:
        if node["attributes"].get("id") == element_id:
            return node
    return None


def find_node_by_backend_id(dom_nodes, backend_id):
    for node in dom_nodes:
        if node["backendNodeId"] == backend_id:
            return node
    return None


def find_node_by_ax_role(ax_nodes, role_name):
    for node in ax_nodes:
        if node["role"] == role_name:
            return node
    return None


def find_focused_ax_node(ax_nodes):
    for node in ax_nodes:
        if node["focused"]:
            return node
    return None


def plan_click(criteria, snapshot_provider, retries=3):
    """
    Plan a click action based on criteria.
    criteria can be e.g.:
      {"id": "box"}
      {"ax_role": "textbox"}
      {"focused": True}
    """
    found_any = False
    for attempt in range(1, retries + 1):
        ax_data, dom_data = snapshot_provider()
        ax_nodes = parse_ax_tree(ax_data)
        dom_nodes = parse_dom_snapshot(dom_data)

        # 1. Check focused node for refusal
        focused_ax = find_focused_ax_node(ax_nodes)
        if focused_ax:
            if is_refused_ax_node(focused_ax):
                return {"kind": "abort", "reason": "refused"}
            if focused_ax.get("backendDOMNodeId") is not None:
                focused_dom = find_node_by_backend_id(dom_nodes, focused_ax["backendDOMNodeId"])
                if focused_dom and is_refused_dom_node(focused_dom):
                    return {"kind": "abort", "reason": "refused"}

        # 2. Locate target node
        target_dom = None
        if "id" in criteria:
            target_dom = find_node_by_id(dom_nodes, criteria["id"])
        elif "ax_role" in criteria:
            ax_match = find_node_by_ax_role(ax_nodes, criteria["ax_role"])
            if ax_match and ax_match.get("backendDOMNodeId") is not None:
                target_dom = find_node_by_backend_id(dom_nodes, ax_match["backendDOMNodeId"])
        elif criteria.get("focused"):
            if focused_ax and focused_ax.get("backendDOMNodeId") is not None:
                target_dom = find_node_by_backend_id(dom_nodes, focused_ax["backendDOMNodeId"])

        if target_dom:
            found_any = True
            if is_refused_dom_node(target_dom):
                return {"kind": "abort", "reason": "refused"}

            if target_dom["is_visible"]:
                return {
                    "kind": "click",
                    "xy": target_dom["center_doc"],
                    "viewport_xy": target_dom["center_viewport"],
                    "attempts": attempt,
                }

    if found_any:
        return {"kind": "abort", "reason": "not_visible"}
    return {"kind": "abort", "reason": "not_found"}


class CDPClient:
    def __init__(
        self,
        fake_script=None,
        fake_mode="echo",
        fake_url="file:///opt/cu/scenes/v3/ff_form_01.html",
        fake_log=None,
        max_frame_bytes=None,
        allowlist_path=None,
    ):
        self.fake_script = fake_script or os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "..",
                "..",
                "docker",
                "cu",
                "tests",
                "fixtures",
                "cdp",
                "fake_chromium.py",
            )
        )
        self.fake_mode = fake_mode
        self.fake_url = fake_url
        self.fake_log = fake_log
        if self.fake_log is None:
            tmp = tempfile.NamedTemporaryFile(delete=False, prefix="fake_log_")
            self.fake_log = tmp.name
            tmp.close()

        self.allowlist = load_allowlist(allowlist_path)
        self.max_frame_bytes = (
            max_frame_bytes
            if max_frame_bytes is not None
            else self.allowlist.get("max_frame_bytes", 104857600)
        )

        self._id_counter = 0
        self.session_id = None
        self.proc = None
        self._r_fd = None
        self._w_fd = None
        self._read_buf = bytearray()

        self._spawn()

    def _spawn(self):
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()

        def _pre():
            os.dup2(r1, 3)
            os.dup2(w2, 4)
            os.set_inheritable(3, True)
            os.set_inheritable(4, True)

        env = dict(os.environ)
        env["FAKE_MODE"] = self.fake_mode
        env["FAKE_URL"] = self.fake_url
        env["FAKE_LOG"] = self.fake_log

        self.proc = subprocess.Popen(
            [sys.executable, "-B", self.fake_script],
            env=env,
            preexec_fn=_pre,
            pass_fds=(3, 4),
        )

        os.close(r1)
        os.close(w2)
        self._w_fd = w1
        self._r_fd = r2

    def _next_id(self):
        self._id_counter += 1
        return self._id_counter

    def _send_raw(self, msg):
        payload = json.dumps(msg).encode("utf-8")
        if len(payload) > self.max_frame_bytes:
            raise OversizeFrameError("outgoing frame exceeds max_frame_bytes", code=5)
        os.write(self._w_fd, payload + b"\0")

    def _read_frame(self, timeout=10.0):
        deadline = time.time() + timeout
        while True:
            idx = self._read_buf.find(b"\0")
            if idx >= 0:
                frame_bytes = bytes(self._read_buf[:idx])
                del self._read_buf[: idx + 1]
                if len(frame_bytes) > self.max_frame_bytes:
                    raise OversizeFrameError("incoming frame exceeds max_frame_bytes", code=5)
                return json.loads(frame_bytes.decode("utf-8"))

            if len(self._read_buf) > self.max_frame_bytes:
                raise OversizeFrameError("incoming frame exceeds max_frame_bytes", code=5)

            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("timeout waiting for CDP frame")

            r_ready, _, _ = select.select([self._r_fd], [], [], max(0.01, remaining))
            if r_ready:
                chunk = os.read(self._r_fd, 65536)
                if not chunk:
                    raise EOFError("pipe closed by fake chromium")
                self._read_buf += chunk

    def connect(self, timeout=10.0):
        req_id = self._next_id()
        msg = {
            "id": req_id,
            "method": "Target.setAutoAttach",
            "params": {
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "flatten": True,
            },
        }
        self._send_raw(msg)

        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = self._read_frame(timeout=deadline - time.time())
            if frame.get("method") == "Target.attachedToTarget":
                params = frame.get("params", {})
                ti = params.get("targetInfo", {})
                if ti.get("type") == "page" and ti.get("url") == self.fake_url:
                    self.session_id = params.get("sessionId")
                    return self.session_id
        raise TimeoutError("Handshake failed to bind target session")

    def send(self, method, params=None, timeout=10.0):
        if method == "Page.navigate":
            raise RefusalError("Page.navigate is forbidden", code=5)

        is_session = False
        if method in self.allowlist.get("browser_methods", {}):
            is_session = False
        elif method in self.allowlist.get("session_methods", {}):
            is_session = True
        else:
            raise RefusalError(f"Method {method} not allowed", code=5)

        req_id = self._next_id()
        msg = {"id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        if is_session:
            if not self.session_id:
                raise RefusalError("Session not bound", code=5)
            msg["sessionId"] = self.session_id

        self._send_raw(msg)

        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = self._read_frame(timeout=deadline - time.time())
            if "id" in frame and frame["id"] == req_id:
                if "error" in frame:
                    raise RuntimeError(f"CDP error: {frame['error']}")
                return frame.get("result", {})

    def close(self):
        if self._w_fd is not None:
            try:
                os.close(self._w_fd)
            except OSError:
                pass
            self._w_fd = None
        if self._r_fd is not None:
            try:
                os.close(self._r_fd)
            except OSError:
                pass
            self._r_fd = None

        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2.0)
            except Exception:
                pass
            self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
