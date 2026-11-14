"""OpenSuperficies CDP client, AX tree query, and action planning over fake CDP pipe."""

import json
import os
import re
import select
import subprocess
import time

ALLOWLIST_PATH = os.path.join("docker", "cu", "oracle", "allowlist.json")
SENSITIVE_RE = re.compile(r"password|passwd|secret|token|cc-|cvv|cvc|card", re.IGNORECASE)


def load_allowlist(path=None):
    if path is None:
        path = ALLOWLIST_PATH
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


class CDPError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class CDPClient:
    def __init__(self, cmd=None, env=None, max_frame_bytes=None, allowlist_path=None, scene_url=None):
        self.allowlist = load_allowlist(allowlist_path)
        self.max_frame_bytes = (
            max_frame_bytes
            if max_frame_bytes is not None
            else self.allowlist.get("max_frame_bytes", 104857600)
        )
        self.scene_url = scene_url or os.environ.get(
            "FAKE_URL", "file:///opt/cu/scenes/v3/ff_form_01.html"
        )
        self.cmd = cmd or [
            "python3",
            "-B",
            os.path.join("docker", "cu", "tests", "fixtures", "cdp", "fake_chromium.py"),
        ]
        self.env = dict(os.environ)
        if env:
            self.env.update(env)

        self.proc = None
        self.pipe_write_fd = None  # fd 3 for child
        self.pipe_read_fd = None   # fd 4 for child
        self.session_id = None
        self.next_id = 1
        self.read_buf = bytearray()

    def start(self):
        # Create pipes for child fd 3 (input to child) and fd 4 (output from child)
        r3, w3 = os.pipe()
        r4, w4 = os.pipe()

        def _pass_fds():
            os.dup2(r3, 3)
            os.dup2(w4, 4)

        try:
            self.proc = subprocess.Popen(
                self.cmd,
                env=self.env,
                preexec_fn=_pass_fds,
                pass_fds=(r3, w4, 3, 4),
            )
        finally:
            os.close(r3)
            os.close(w4)

        self.pipe_write_fd = w3
        self.pipe_read_fd = r4

        # Handshake if mode is not oversize
        if self.env.get("FAKE_MODE") != "oversize":
            self._handshake()

    def _handshake(self):
        # Target.setAutoAttach
        req_id = self.send_request(
            "Target.setAutoAttach",
            params={
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "flatten": True,
            },
            raw_send=True,
        )

        # Wait for Target.attachedToTarget matching page and url
        deadline = time.time() + 10.0
        attached_session = None

        while time.time() < deadline:
            msg = self._read_frame(timeout=max(0.1, deadline - time.time()))
            if msg is None:
                continue
            if "method" in msg and msg["method"] == "Target.attachedToTarget":
                params = msg.get("params", {})
                ti = params.get("targetInfo", {})
                if ti.get("type") == "page" and ti.get("url") == self.scene_url:
                    attached_session = params.get("sessionId")
                    break

        if not attached_session:
            raise CDPError(5, "Handshake failed: Target.attachedToTarget not received")

        self.session_id = attached_session

    def send_request(self, method, params=None, session_id=None, raw_send=False, timeout=10.0):
        # Local allowlist enforcement
        if method == "Page.navigate":
            raise CDPError(5, f"Method {method} refused by local allow-list")

        browser_methods = self.allowlist.get("browser_methods", {})
        session_methods = self.allowlist.get("session_methods", {})

        if method not in browser_methods and method not in session_methods:
            raise CDPError(5, f"Method {method} not on allow-list")

        req_id = self.next_id
        self.next_id += 1

        payload = {"id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        sid = session_id or (self.session_id if not raw_send else None)
        if sid:
            payload["sessionId"] = sid

        frame_data = json.dumps(payload).encode("utf-8") + b"\0"
        os.write(self.pipe_write_fd, frame_data)

        if raw_send and method == "Target.setAutoAttach":
            return req_id

        # Wait for response matching req_id
        deadline = time.time() + min(timeout, 10.0)
        while time.time() < deadline:
            msg = self._read_frame(timeout=max(0.1, deadline - time.time()))
            if msg is None:
                continue
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise CDPError(msg["error"].get("code", 5), msg["error"].get("message", "CDP Error"))
                return msg.get("result", {})

        raise CDPError(5, f"Timeout waiting for response to {method} (id {req_id})")

    def _read_frame(self, timeout=10.0):
        deadline = time.time() + timeout
        while b"\0" not in self.read_buf:
            rem = deadline - time.time()
            if rem <= 0:
                if len(self.read_buf) > self.max_frame_bytes:
                    raise CDPError(5, "Oversize frame received")
                return None
            r_ready, _, _ = select.select([self.pipe_read_fd], [], [], rem)
            if not r_ready:
                if len(self.read_buf) > self.max_frame_bytes:
                    raise CDPError(5, "Oversize frame received")
                return None
            try:
                data = os.read(self.pipe_read_fd, 65536)
                if not data:
                    break
                self.read_buf.extend(data)
                if len(self.read_buf) > self.max_frame_bytes:
                    raise CDPError(5, "Oversize frame received")
            except OSError:
                break

        if b"\0" in self.read_buf:
            idx = self.read_buf.find(b"\0")
            if idx > self.max_frame_bytes:
                raise CDPError(5, "Oversize frame received")
            frame_bytes = bytes(self.read_buf[:idx])
            del self.read_buf[: idx + 1]
            try:
                return json.loads(frame_bytes.decode("utf-8"))
            except Exception:
                return None
        elif len(self.read_buf) > self.max_frame_bytes:
            raise CDPError(5, "Oversize frame received")

        return None

    def read_oversize(self, timeout=2.0):
        """Method specifically to test reading oversize frame from pipe."""
        return self._read_frame(timeout=timeout)

    def close(self):
        if self.pipe_write_fd is not None:
            try:
                os.close(self.pipe_write_fd)
            except OSError:
                pass
            self.pipe_write_fd = None

        if self.pipe_read_fd is not None:
            try:
                os.close(self.pipe_read_fd)
            except OSError:
                pass
            self.pipe_read_fd = None

        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.0)
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=1.0)
                except Exception:
                    pass
            self.proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# Pure query functions
def parse_ax_tree(ax_data):
    """Parses Accessibility.getFullAXTree response."""
    if not isinstance(ax_data, dict):
        return []
    return ax_data.get("nodes", [])


def find_focused_ax_node(ax_nodes):
    for node in ax_nodes:
        props = node.get("properties", [])
        for prop in props:
            if prop.get("name") == "focused":
                val = prop.get("value", {})
                if isinstance(val, dict) and val.get("value") is True:
                    return node
    return None


def find_ax_node_by_role(ax_nodes, role):
    for node in ax_nodes:
        r = node.get("role", {})
        if isinstance(r, dict) and r.get("value") == role:
            return node
    return None


def parse_dom_snapshot(snapshot_data):
    """Parses DOMSnapshot.captureSnapshot result."""
    if not isinstance(snapshot_data, dict):
        return []
    docs = snapshot_data.get("documents", [])
    strings = snapshot_data.get("strings", [])
    parsed_nodes = []

    for doc in docs:
        scroll_x = doc.get("scrollOffsetX", 0)
        scroll_y = doc.get("scrollOffsetY", 0)
        nodes = doc.get("nodes", {})
        layout = doc.get("layout", {})

        backend_ids = nodes.get("backendNodeId", [])
        attributes_list = nodes.get("attributes", [])

        layout_node_indices = layout.get("nodeIndex", [])
        layout_bounds = layout.get("bounds", [])
        layout_styles = layout.get("styles", [])

        # Build nodeIndex -> layout_index mapping
        layout_map = {node_idx: i for i, node_idx in enumerate(layout_node_indices)}

        for i, backend_id in enumerate(backend_ids):
            attrs_raw = attributes_list[i] if i < len(attributes_list) else []
            attrs = {}
            for j in range(0, len(attrs_raw), 2):
                if j + 1 < len(attrs_raw):
                    k_idx, v_idx = attrs_raw[j], attrs_raw[j + 1]
                    k_str = strings[k_idx] if k_idx < len(strings) else ""
                    v_str = strings[v_idx] if v_idx < len(strings) else ""
                    attrs[k_str] = v_str

            layout_idx = layout_map.get(i)
            bounds = None
            computed_styles = {}
            is_visible = False

            if layout_idx is not None:
                if layout_idx < len(layout_bounds):
                    bounds = layout_bounds[layout_idx]
                if layout_idx < len(layout_styles):
                    style_indices = layout_styles[layout_idx]
                    # computedStyles requested: ["visibility", "opacity", "display"]
                    if len(style_indices) >= 3:
                        computed_styles["visibility"] = (
                            strings[style_indices[0]] if style_indices[0] < len(strings) else ""
                        )
                        computed_styles["opacity"] = (
                            strings[style_indices[1]] if style_indices[1] < len(strings) else ""
                        )
                        computed_styles["display"] = (
                            strings[style_indices[2]] if style_indices[2] < len(strings) else ""
                        )

            if bounds and len(bounds) == 4:
                x, y, w, h = bounds
                vis = computed_styles.get("visibility", "visible") == "visible"
                disp = computed_styles.get("display", "") != "none"
                op_str = computed_styles.get("opacity", "1")
                try:
                    op_val = float(op_str)
                except ValueError:
                    op_val = 1.0
                op = op_val != 0.0
                pos_dim = w > 0 and h > 0
                is_visible = vis and disp and op and pos_dim

            parsed_nodes.append({
                "backendNodeId": backend_id,
                "attributes": attrs,
                "bounds": bounds,
                "computedStyles": computed_styles,
                "visible": is_visible,
                "scrollOffsetX": scroll_x,
                "scrollOffsetY": scroll_y,
            })

    return parsed_nodes


def find_snapshot_node_by_id(parsed_nodes, attr_id):
    for node in parsed_nodes:
        if node["attributes"].get("id") == attr_id:
            return node
    return None


def is_sensitive(node_dict, ax_node=None):
    # Check type attribute
    attrs = node_dict.get("attributes", {}) if node_dict else {}
    attr_type = attrs.get("type", "").lower()
    if attr_type in ("password", "file"):
        return True

    # Check strings: id, name, AX name
    check_strings = [
        attrs.get("id", ""),
        attrs.get("name", ""),
    ]
    if ax_node:
        name_val = ax_node.get("name", {})
        if isinstance(name_val, dict):
            check_strings.append(name_val.get("value", ""))
        elif isinstance(name_val, str):
            check_strings.append(name_val)

    for s in check_strings:
        if s and SENSITIVE_RE.search(s):
            return True

    return False


def plan_click(target_query, snapshot_provider, retries=3):
    """Action planning: plan click with retries."""
    attempt = 0
    for attempt in range(1, retries + 1):
        data = snapshot_provider()
        ax_tree = parse_ax_tree(data.get("ax", {}))
        snapshot_nodes = parse_dom_snapshot(data.get("snapshot", {}))

        # Identify target node
        node = None
        ax_node = None

        if "id" in target_query:
            node = find_snapshot_node_by_id(snapshot_nodes, target_query["id"])
            if node:
                # Find matching AX node if any
                for axn in ax_tree:
                    if str(axn.get("nodeId")) == str(node["backendNodeId"]):
                        ax_node = axn
                        break
        elif "role" in target_query:
            ax_node = find_ax_node_by_role(ax_tree, target_query["role"])
            if ax_node:
                for sn in snapshot_nodes:
                    if str(sn["backendNodeId"]) == str(ax_node.get("nodeId")):
                        node = sn
                        break
        elif target_query.get("focused"):
            ax_node = find_focused_ax_node(ax_tree)
            if ax_node:
                for sn in snapshot_nodes:
                    if str(sn["backendNodeId"]) == str(ax_node.get("nodeId")):
                        node = sn
                        break

        # Check refusal first if node / ax_node exists
        if (node and is_sensitive(node, ax_node)) or (ax_node and is_sensitive({}, ax_node)):
            return {"kind": "abort", "reason": "refused", "attempts": attempt}

        if node is not None:
            if not node["visible"]:
                continue
            # Calculate coordinates
            bounds = node["bounds"]
            x, y, w, h = bounds
            doc_x = x + (w // 2)
            doc_y = y + (h // 2)
            scroll_x = node["scrollOffsetX"]
            scroll_y = node["scrollOffsetY"]
            vp_x = doc_x - scroll_x
            vp_y = doc_y - scroll_y

            return {
                "kind": "click",
                "xy": [doc_x, doc_y],
                "viewport_xy": [vp_x, vp_y],
                "attempts": attempt,
            }

    # If node was found but not visible, or not found at all
    # Check if node existed at last attempt
    if node is not None:
        return {"kind": "abort", "reason": "not_visible", "attempts": attempt or retries}
    return {"kind": "abort", "reason": "not_found", "attempts": attempt or retries}
