"""CDP Client and AX/DOM Query/Action Planning for X3 py_s3 seed."""

import os
import sys
import json
import re
import subprocess
import time
import select

E_GATE = 5

DEFAULT_ALLOWLIST_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "docker", "cu", "oracle", "allowlist.json")
)

DEFAULT_FAKE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "docker", "cu", "tests", "fixtures", "cdp", "fake_chromium.py")
)

SENSITIVE_PATTERNS = [
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"passwd", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"cc-", re.IGNORECASE),
    re.compile(r"card.?[0-9]*", re.IGNORECASE),
    re.compile(r"cvv", re.IGNORECASE),
    re.compile(r"cvc", re.IGNORECASE),
]


class LocalRefusalError(Exception):
    def __init__(self, message="Method refused by local allowlist", code=E_GATE):
        super().__init__(message)
        self.code = code


def load_allowlist(path=None):
    if path is None:
        path = DEFAULT_ALLOWLIST_PATH
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class CDPClient:
    def __init__(self, fake_script_path=None, fake_url=None, fake_mode="echo", fake_log=None, max_frame_bytes=None, allowlist_path=None):
        self.allowlist = load_allowlist(allowlist_path)
        self.max_frame_bytes = max_frame_bytes or self.allowlist.get("max_frame_bytes", 104857600)

        if fake_script_path is None:
            fake_script_path = DEFAULT_FAKE_PATH
        if fake_url is None:
            fake_url = "file:///opt/cu/scenes/v3/ff_form_01.html"

        self.fake_url = fake_url
        self.next_id = 1
        self.session_id = None
        self.proc = None
        self.write_fd = None
        self.read_fd = None

        env = dict(os.environ)
        env["FAKE_MODE"] = fake_mode
        env["FAKE_URL"] = fake_url
        if fake_log:
            env["FAKE_LOG"] = fake_log

        # Set up pipes
        r_out, w_out = os.pipe() # parent reads from child's fd 4
        r_in, w_in = os.pipe()   # parent writes to child's fd 3

        def _pass_fds():
            os.dup2(r_in, 3)
            os.dup2(w_out, 4)

        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-B", fake_script_path],
                env=env,
                preexec_fn=_pass_fds,
                pass_fds=(3, 4),
                close_fds=True,
            )
            os.close(r_in)
            os.close(w_out)

            self.write_fd = w_in
            self.read_fd = r_out
            self.read_buf = b""

            # Do handshake
            self._handshake()
        except Exception:
            self.close()
            raise

    def _handshake(self):
        # Send Target.setAutoAttach
        res = self.send_request("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True
        })

        # Wait for Target.attachedToTarget matching targetInfo.type == 'page' and url == fake_url
        start_time = time.time()
        while self.session_id is None and (time.time() - start_time < 10.0):
            frame = self._read_frame(timeout=10.0)
            if not frame:
                break
            if frame.get("method") == "Target.attachedToTarget":
                params = frame.get("params", {})
                tinfo = params.get("targetInfo", {})
                if tinfo.get("type") == "page" and tinfo.get("url") == self.fake_url:
                    self.session_id = params.get("sessionId")
                    break

    def _write_frame(self, data):
        raw = json.dumps(data).encode("utf-8") + b"\0"
        os.write(self.write_fd, raw)

    def _read_frame(self, timeout=10.0):
        start = time.time()
        while b"\0" not in self.read_buf:
            rem = timeout - (time.time() - start)
            if rem <= 0:
                raise TimeoutError("Timed out reading frame")
            r, _, _ = select.select([self.read_fd], [], [], max(0.1, rem))
            if r:
                chunk = os.read(self.read_fd, 65536)
                if not chunk:
                    break
                self.read_buf += chunk
                if len(self.read_buf) > self.max_frame_bytes and b"\0" not in self.read_buf:
                    raise ValueError(f"Incoming frame exceeds max_frame_bytes ({self.max_frame_bytes})")

        if b"\0" in self.read_buf:
            idx = self.read_buf.index(b"\0")
            frame_bytes = self.read_buf[:idx]
            self.read_buf = self.read_buf[idx + 1:]
            if len(frame_bytes) > self.max_frame_bytes:
                raise ValueError(f"Incoming frame exceeds max_frame_bytes ({self.max_frame_bytes})")
            return json.loads(frame_bytes.decode("utf-8"))
        return None

    def is_method_allowed(self, method):
        if method == "Page.navigate":
            return False
        browser_methods = self.allowlist.get("browser_methods", {})
        session_methods = self.allowlist.get("session_methods", {})
        return (method in browser_methods) or (method in session_methods)

    def send_request(self, method, params=None, session_id=None):
        if not self.is_method_allowed(method):
            raise LocalRefusalError(f"Method {method} refused by local allowlist", code=E_GATE)

        req_id = self.next_id
        self.next_id += 1

        req = {"id": req_id, "method": method}
        if params is not None:
            req["params"] = params
        sid = session_id or self.session_id
        if sid:
            req["sessionId"] = sid

        self._write_frame(req)

        start = time.time()
        while time.time() - start < 10.0:
            frame = self._read_frame(timeout=10.0 - (time.time() - start))
            if not frame:
                continue
            if "id" in frame and frame["id"] == req_id:
                if "error" in frame:
                    return {"error": frame["error"]}
                return frame.get("result", {})
            # Handle event during call
            if frame.get("method") == "Target.attachedToTarget":
                p = frame.get("params", {})
                tinfo = p.get("targetInfo", {})
                if tinfo.get("type") == "page" and tinfo.get("url") == self.fake_url:
                    self.session_id = p.get("sessionId")

        raise TimeoutError(f"Response for request {req_id} timed out")

    def close(self):
        try:
            if self.write_fd is not None:
                os.close(self.write_fd)
                self.write_fd = None
        except Exception:
            pass
        try:
            if self.read_fd is not None:
                os.close(self.read_fd)
                self.read_fd = None
        except Exception:
            pass
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait()
            except Exception:
                pass
            self.proc = None


# Pure Functions for Query / Parsing

def parse_ax_tree(ax_result):
    """
    Parse Accessibility.getFullAXTree result.
    Returns list of node dicts.
    """
    if not ax_result or "nodes" not in ax_result:
        return []
    return ax_result["nodes"]


def parse_dom_snapshot(snapshot_result):
    """
    Parse DOMSnapshot.captureSnapshot result.
    Returns parsed structure with document metadata, nodes, layout rects & styles.
    """
    if not snapshot_result or "documents" not in snapshot_result or not snapshot_result["documents"]:
        return {"nodes": [], "scrollOffsetX": 0, "scrollOffsetY": 0}

    doc = snapshot_result["documents"][0]
    strings = snapshot_result.get("strings", [])
    scroll_x = doc.get("scrollOffsetX", 0)
    scroll_y = doc.get("scrollOffsetY", 0)

    nodes_data = doc.get("nodes", {})
    backend_ids = nodes_data.get("backendNodeId", [])
    attributes_list = nodes_data.get("attributes", [])
    node_names = nodes_data.get("nodeName", [])
    node_types = nodes_data.get("nodeType", [])

    layout_data = doc.get("layout", {})
    layout_node_indices = layout_data.get("nodeIndex", [])
    bounds_list = layout_data.get("bounds", [])
    styles_list = layout_data.get("styles", [])

    # Map layout info by node index in nodes array
    node_index_to_layout = {}
    for l_idx, n_idx in enumerate(layout_node_indices):
        bounds = bounds_list[l_idx] if l_idx < len(bounds_list) else None
        styles = styles_list[l_idx] if l_idx < len(styles_list) else None
        node_index_to_layout[n_idx] = {
            "bounds": bounds,
            "styles": styles
        }

    parsed_nodes = []
    for idx, backend_id in enumerate(backend_ids):
        # Resolve attributes
        raw_attrs = attributes_list[idx] if idx < len(attributes_list) else []
        attrs = {}
        # attributes array contains string table indices in pairs [name_idx, val_idx, ...]
        for i in range(0, len(raw_attrs), 2):
            if i + 1 < len(raw_attrs):
                k = strings[raw_attrs[i]] if raw_attrs[i] < len(strings) else ""
                v = strings[raw_attrs[i + 1]] if raw_attrs[i + 1] < len(strings) else ""
                attrs[k] = v

        node_name = strings[node_names[idx]] if idx < len(node_names) and node_names[idx] < len(strings) else ""
        node_type = node_types[idx] if idx < len(node_types) else None

        layout_info = node_index_to_layout.get(idx, {})
        bounds = layout_info.get("bounds")
        styles = layout_info.get("styles")

        # Resolve computed styles (visibility, opacity, display)
        computed_styles = {}
        if styles:
            # Computed styles requested were ["visibility", "opacity", "display"]
            # styles are indices into strings
            style_vals = [strings[s_idx] if s_idx < len(strings) else "" for s_idx in styles]
            if len(style_vals) >= 1:
                computed_styles["visibility"] = style_vals[0]
            if len(style_vals) >= 2:
                computed_styles["opacity"] = style_vals[1]
            if len(style_vals) >= 3:
                computed_styles["display"] = style_vals[2]

        parsed_nodes.append({
            "index": idx,
            "backendNodeId": backend_id,
            "nodeName": node_name,
            "nodeType": node_type,
            "attributes": attrs,
            "bounds": bounds,
            "styles": computed_styles,
        })

    return {
        "nodes": parsed_nodes,
        "scrollOffsetX": scroll_x,
        "scrollOffsetY": scroll_y,
    }


def is_node_visible(node):
    if not node:
        return False
    bounds = node.get("bounds")
    if not bounds or len(bounds) < 4:
        return False
    x, y, w, h = bounds
    if w <= 0 or h <= 0:
        return False

    styles = node.get("styles", {})
    visibility = styles.get("visibility", "visible")
    opacity = styles.get("opacity", "1")
    display = styles.get("display", "block")

    if visibility != "visible":
        return False
    if display == "none":
        return False
    try:
        if float(opacity) == 0.0:
            return False
    except ValueError:
        pass

    return True


def find_dom_node_by_id(parsed_dom, target_id):
    for node in parsed_dom.get("nodes", []):
        if node.get("attributes", {}).get("id") == target_id:
            return node
    return None


def find_ax_node_by_role(ax_tree, role_name):
    for node in ax_tree:
        role_info = node.get("role", {})
        if isinstance(role_info, dict) and role_info.get("value") == role_name:
            return node
        elif isinstance(role_info, str) and role_info == role_name:
            return node
    return None


def find_focused_ax_node(ax_tree):
    for node in ax_tree:
        properties = node.get("properties", [])
        for prop in properties:
            if prop.get("name") == "focused":
                val = prop.get("value", {})
                if isinstance(val, dict) and val.get("value") is True:
                    return node
                elif val is True:
                    return node
    return None


def check_refusal(dom_node=None, ax_node=None):
    """
    Check if node triggers local refusal rule based on sensitive input types/names/ids/AX names.
    Returns True if refused, False otherwise.
    Refusal: if the target or the focused node has attribute type password or file,
    or an id, name or AX name matching case-insensitively password, passwd, secret, token, cc-,
    card followed by optional char and num, cvv or cvc, return kind abort reason refused before any click is planned.
    """
    check_strings = []

    if dom_node:
        attrs = dom_node.get("attributes", {})
        input_type = attrs.get("type", "").lower()
        if input_type in ("password", "file"):
            return True
        for key in ("id", "name"):
            if key in attrs:
                check_strings.append(attrs[key])

    if ax_node:
        ax_name = ax_node.get("name", {})
        if isinstance(ax_name, dict):
            check_strings.append(ax_name.get("value", ""))
        elif isinstance(ax_name, str):
            check_strings.append(ax_name)

        ax_role = ax_node.get("role", {})
        role_val = ax_role.get("value", "") if isinstance(ax_role, dict) else str(ax_role)

        for prop in ax_node.get("properties", []):
            pname = prop.get("name")
            pval = prop.get("value", {})
            pstr = pval.get("value", "") if isinstance(pval, dict) else str(pval)
            if pname in ("name", "description", "value"):
                check_strings.append(str(pstr))

    for s in check_strings:
        if not s:
            continue
        for pat in SENSITIVE_PATTERNS:
            if pat.search(s):
                return True

    return False


def plan_click(provider, query_spec, max_retries=3):
    """
    Action planning for click on a specified node query.
    query_spec can be:
      - {"id": "box"}
      - {"role": "button"}
      - {"focused": True}
    Returns dictionary with action result.
    """
    attempts = 0
    while attempts < max_retries:
        attempts += 1
        snapshot, ax_tree_data = provider()
        parsed_dom = parse_dom_snapshot(snapshot)
        parsed_ax = parse_ax_tree(ax_tree_data)

        dom_node = None
        ax_node = None

        if "id" in query_spec:
            target_id = query_spec["id"]
            dom_node = find_dom_node_by_id(parsed_dom, target_id)
            if dom_node:
                b_id = str(dom_node.get("backendNodeId"))
                for n in parsed_ax:
                    if str(n.get("backendNodeId")) == b_id or str(n.get("nodeId")) == b_id:
                        ax_node = n
                        break
        elif "role" in query_spec:
            ax_node = find_ax_node_by_role(parsed_ax, query_spec["role"])
            if ax_node:
                b_id = ax_node.get("backendNodeId") or ax_node.get("nodeId")
                for n in parsed_dom.get("nodes", []):
                    if str(n.get("backendNodeId")) == str(b_id):
                        dom_node = n
                        break
        elif query_spec.get("focused"):
            ax_node = find_focused_ax_node(parsed_ax)
            if ax_node:
                b_id = ax_node.get("backendNodeId") or ax_node.get("nodeId")
                for n in parsed_dom.get("nodes", []):
                    if str(n.get("backendNodeId")) == str(b_id):
                        dom_node = n
                        break

        # Always check refusal on target node or focused node if present or specified
        if query_spec.get("focused") and ax_node:
            if check_refusal(dom_node=dom_node, ax_node=ax_node):
                return {"kind": "abort", "reason": "refused"}

        if dom_node or ax_node:
            if check_refusal(dom_node=dom_node, ax_node=ax_node):
                return {"kind": "abort", "reason": "refused"}

        if not dom_node and not ax_node:
            continue

        if dom_node:
            if is_node_visible(dom_node):
                bounds = dom_node["bounds"]
                x, y, w, h = bounds[0], bounds[1], bounds[2], bounds[3]
                cx = int(x) + int(w) // 2
                cy = int(y) + int(h) // 2
                scroll_x = parsed_dom.get("scrollOffsetX", 0)
                scroll_y = parsed_dom.get("scrollOffsetY", 0)
                vx = cx - int(scroll_x)
                vy = cy - int(scroll_y)
                return {
                    "kind": "click",
                    "xy": [cx, cy],
                    "viewport_xy": [vx, vy],
                    "attempts": attempts,
                }
            else:
                # Exists but not visible
                pass

    # If loop finished without finding visible node
    if dom_node is not None or ax_node is not None:
        return {"kind": "abort", "reason": "not_visible"}
    return {"kind": "abort", "reason": "not_found"}
