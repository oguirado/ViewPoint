#!/usr/bin/env python3
"""
LANG-X X2 browser seed (py_w3)
Browser-interaction CLI tool using Python standard library only.
"""

import sys
import os
import json
import time
import math
import select
import shutil
import signal
import tempfile
import argparse
import subprocess


def canonical_dumps(obj):
    """
    Canonical serialization:
    Object keys sorted by code point, separators comma and colon with no spaces,
    every non-ASCII character escaped as a backslash u escape with four lowercase hex digits
    and surrogate pairs above U+FFFF, control characters written with short escapes or \\u00XX,
    slash never escaped, no floats anywhere in stdout.
    Matches json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(',', ':')).
    """
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def parse_box(bounds):
    """
    Compute closed integer box [x0, y0, x1, y1] or None if width or height is <= 0.
    bounds is [x, y, width, height] from DOMSnapshot layout bounds.
    Rule R10:
    x0 = floor(x)
    y0 = floor(y)
    x1 = max(x0, ceil(x + width - 1))
    y1 = max(y0, ceil(y + height - 1))
    """
    if not bounds or len(bounds) < 4:
        return None
    x, y, w, h = bounds[0], bounds[1], bounds[2], bounds[3]
    if w <= 0 or h <= 0:
        return None
    x0 = math.floor(x)
    y0 = math.floor(y)
    x1 = max(x0, math.ceil(x + w - 1.0))
    y1 = max(y0, math.ceil(y + h - 1.0))
    return [int(x0), int(y0), int(x1), int(y1)]


def normalize_whitespace(text):
    if not text:
        return ""
    words = text.split()
    res = " ".join(words)
    return res[:120]


def is_actionable(tag, role, box, is_disabled, attributes_dict):
    """
    act true when box is not null, the element has no disabled attribute, and either:
    - tag is a with an href attribute, button, select, textarea, summary, or input whose type is not hidden
    - role is one of button, link, checkbox, radio, textbox, searchbox, combobox, menuitem, tab, switch, option, slider or spinbutton
    false otherwise.
    """
    if box is None:
        return False
    if is_disabled:
        return False

    tag = (tag or "").lower()
    role = (role or "").lower()

    if tag in ("button", "select", "textarea", "summary"):
        return True
    if tag == "a" and "href" in attributes_dict:
        return True
    if tag == "input":
        input_type = (attributes_dict.get("type") or "").lower()
        if input_type != "hidden":
            return True

    act_roles = {
        "button", "link", "checkbox", "radio", "textbox",
        "searchbox", "combobox", "menuitem", "tab", "switch",
        "option", "slider", "spinbutton"
    }
    if role in act_roles:
        return True

    return False


def get_backoff_sequence(timeout_ms):
    """
    Computes sequence of backoff delays in ms capped at 64ms under timeout_ms.
    """
    delays = []
    total = 0
    backoff = 1
    while total < timeout_ms:
        d = min(backoff, timeout_ms - total)
        delays.append(d)
        total += d
        backoff = min(backoff * 2, 64)
    return delays, total


class PipeCDPClient:
    def __init__(self, chrome_bin, user_data_dir, global_deadline=None):
        self.chrome_bin = chrome_bin
        self.user_data_dir = user_data_dir
        self.global_deadline = global_deadline
        self.proc = None
        self.p1_w = None  # Parent write end (child fd 3)
        self.p2_r = None  # Parent read end (child fd 4)
        self.buf = bytearray()
        self.next_id = 1
        self.outstanding_ids = set()
        self.pending_responses = {}
        self.pending_events = []

    def start(self):
        p1_r, p1_w = os.pipe()
        p2_r, p2_w = os.pipe()

        def setup_child():
            os.dup2(p1_r, 3)
            os.dup2(p2_w, 4)

        cmd = [
            self.chrome_bin,
            "--headless=new",
            "--remote-debugging-pipe",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-extensions",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--window-size=1280,800",
            f"--user-data-dir={self.user_data_dir}",
            "about:blank"
        ]

        try:
            self.proc = subprocess.Popen(
                cmd,
                pass_fds=(3, 4),
                preexec_fn=setup_child,
                start_new_session=True
            )
        except Exception:
            sys.exit(3)

        os.close(p1_r)
        os.close(p2_w)
        self.p1_w = p1_w
        self.p2_r = p2_r

    def poll_reads(self, timeout_s=0.1):
        if self.global_deadline is not None and time.monotonic() >= self.global_deadline:
            sys.exit(5)

        r, _, _ = select.select([self.p2_r], [], [], max(0.001, timeout_s))
        if r:
            chunk = os.read(self.p2_r, 65536)
            if not chunk:
                if len(self.buf) > 0:
                    sys.exit(6)
                # Pipe closed unexpectedly
                sys.exit(4)

            self.buf.extend(chunk)
            while b"\x00" in self.buf:
                idx = self.buf.index(b"\x00")
                raw = self.buf[:idx]
                del self.buf[:idx + 1]
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except Exception:
                    sys.exit(7)

                # Rule R14 validation
                if "id" in msg:
                    req_id = msg.get("id")
                    if not isinstance(req_id, int) or isinstance(req_id, bool):
                        sys.exit(7)
                    if req_id not in self.outstanding_ids:
                        sys.exit(7)
                    if "result" not in msg and "error" not in msg:
                        sys.exit(7)
                    self.outstanding_ids.remove(req_id)
                    self.pending_responses[req_id] = msg
                elif "method" in msg:
                    self.pending_events.append(msg)
                else:
                    sys.exit(7)
        return True

    def send_command(self, method, params=None, session_id=None):
        msg_id = self.next_id
        self.next_id += 1
        self.outstanding_ids.add(msg_id)
        req = {"id": msg_id, "method": method}
        if params is not None:
            req["params"] = params
        if session_id is not None:
            req["sessionId"] = session_id

        payload = json.dumps(req).encode("utf-8") + b"\x00"
        try:
            os.write(self.p1_w, payload)
        except Exception:
            sys.exit(3)
        return msg_id

    def send_pipeline(self, cmds):
        msg_ids = []
        for method, params, session_id in cmds:
            msg_ids.append(self.send_command(method, params, session_id))
        return msg_ids

    def wait_for_response(self, req_id, timeout_s=30.0):
        start = time.monotonic()
        while req_id not in self.pending_responses:
            now = time.monotonic()
            if self.global_deadline is not None and now >= self.global_deadline:
                sys.exit(5)
            if now - start > timeout_s:
                sys.exit(5)
            self.poll_reads(0.01)
        res = self.pending_responses.pop(req_id)
        return res

    def wait_for_event(self, method, session_id=None, timeout_s=30.0):
        start = time.monotonic()
        while True:
            for i, ev in enumerate(self.pending_events):
                if ev.get("method") == method:
                    if session_id is None or ev.get("sessionId") == session_id:
                        return self.pending_events.pop(i)
            now = time.monotonic()
            if self.global_deadline is not None and now >= self.global_deadline:
                sys.exit(5)
            if now - start > timeout_s:
                sys.exit(5)
            self.poll_reads(0.01)

    def close(self):
        try:
            if self.p1_w is not None:
                req = {"id": self.next_id, "method": "Browser.close"}
                payload = json.dumps(req).encode("utf-8") + b"\x00"
                os.write(self.p1_w, payload)
        except Exception:
            pass

        if self.proc is not None:
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass

        if self.p1_w is not None:
            try:
                os.close(self.p1_w)
            except Exception:
                pass
        if self.p2_r is not None:
            try:
                os.close(self.p2_r)
            except Exception:
                pass


def parse_nodes_and_capture(snap_result, ax_result):
    strings = snap_result.get("strings", [])
    documents = snap_result.get("documents", [])
    if not documents:
        return []

    doc0 = documents[0]
    nodes = doc0.get("nodes", {})
    layout = doc0.get("layout", {})

    node_names = nodes.get("nodeName", [])
    backend_node_ids = nodes.get("backendNodeId", [])
    attributes_list = nodes.get("attributes", [])
    node_types = nodes.get("nodeType", [])
    input_values = nodes.get("inputValue", {})
    input_checked_map = nodes.get("inputChecked", {})
    option_selected_map = nodes.get("optionSelected", {})

    layout_node_indices = layout.get("nodeIndex", [])
    layout_bounds = layout.get("bounds", [])

    ax_map = {}
    for ax_node in ax_result.get("nodes", []):
        b_id = ax_node.get("backendDOMNodeId")
        if b_id is not None:
            role_val = ax_node.get("role", {}).get("value") or ""
            name_val = ax_node.get("name", {}).get("value") or ""
            ignored = ax_node.get("ignored", False)
            ax_map[b_id] = {
                "role": role_val,
                "name": name_val,
                "ignored": ignored
            }

    layout_map = {}
    for l_idx, n_idx in enumerate(layout_node_indices):
        if l_idx < len(layout_bounds):
            layout_map[n_idx] = layout_bounds[l_idx]

    def get_value_from_snapshot(n_idx, tag):
        val_idx = None
        if isinstance(input_values, dict):
            indices = input_values.get("index", [])
            vals = input_values.get("value", [])
            if n_idx in indices:
                pos = indices.index(n_idx)
                if pos < len(vals):
                    val_idx = vals[pos]
        if val_idx is not None and 0 <= val_idx < len(strings):
            return strings[val_idx]
        return ""

    def is_checked(n_idx, tag, attrs_dict):
        if isinstance(input_checked_map, dict):
            indices = input_checked_map.get("index", [])
            if n_idx in indices:
                return True
        if isinstance(option_selected_map, dict):
            indices = option_selected_map.get("index", [])
            if n_idx in indices:
                return True
        return False

    emitted_records = []
    skipped_tags = {
        "html", "head", "script", "style", "meta", "link", "title", "noscript"
    }

    num_nodes = len(node_names)
    for n_idx in range(num_nodes):
        if n_idx not in layout_map:
            continue

        ntype = node_types[n_idx] if n_idx < len(node_types) else None
        if ntype != 1:
            continue

        tag_str_idx = node_names[n_idx]
        raw_tag = strings[tag_str_idx] if 0 <= tag_str_idx < len(strings) else ""
        tag = raw_tag.lower()

        if tag in skipped_tags:
            continue

        backend_id = backend_node_ids[n_idx] if n_idx < len(backend_node_ids) else None

        attrs_dict = {}
        if n_idx < len(attributes_list):
            attr_pair_indices = attributes_list[n_idx]
            if attr_pair_indices:
                for k in range(0, len(attr_pair_indices) - 1, 2):
                    k_idx = attr_pair_indices[k]
                    v_idx = attr_pair_indices[k + 1]
                    k_str = strings[k_idx] if 0 <= k_idx < len(strings) else ""
                    v_str = strings[v_idx] if 0 <= v_idx < len(strings) else ""
                    attrs_dict[k_str.lower()] = v_str

        id_attr = attrs_dict.get("id", "")

        ax_info = ax_map.get(backend_id)
        role = ""
        name = ""
        if ax_info and not ax_info.get("ignored", False):
            role = ax_info.get("role") or ""
            name = ax_info.get("name") or ""

        name = normalize_whitespace(name)

        value = ""
        if tag in ("input", "textarea", "select"):
            value = get_value_from_snapshot(n_idx, tag)
            if not value and tag == "input" and "value" in attrs_dict:
                value = attrs_dict["value"]

        checked = False
        if tag == "input":
            input_type = attrs_dict.get("type", "").lower()
            if input_type in ("checkbox", "radio"):
                checked = is_checked(n_idx, tag, attrs_dict) or ("checked" in attrs_dict)

        bounds = layout_map[n_idx]
        box = parse_box(bounds)

        is_disabled = "disabled" in attrs_dict
        act = is_actionable(tag, role, box, is_disabled, attrs_dict)

        rec = {
            "i": len(emitted_records),
            "tag": tag,
            "id": id_attr,
            "role": role,
            "name": name,
            "value": value,
            "checked": checked,
            "box": box,
            "act": act
        }
        emitted_records.append(rec)

    return emitted_records


def resolve_step_target(client, session_id, selector, timeout_ms):
    start_time = time.monotonic()
    timeout_s = timeout_ms / 1000.0
    backoff_ms = 1

    while True:
        if client.global_deadline is not None and time.monotonic() >= client.global_deadline:
            sys.exit(5)

        doc_msg_id = client.send_command("DOM.getDocument", {"depth": 1}, session_id=session_id)
        doc_res = client.wait_for_response(doc_msg_id)
        root_node_id = doc_res.get("result", {}).get("root", {}).get("nodeId", 0)

        if root_node_id != 0:
            qs_msg_id = client.send_command("DOM.querySelector", {"nodeId": root_node_id, "selector": selector}, session_id=session_id)
            qs_res = client.wait_for_response(qs_msg_id)
            node_id = qs_res.get("result", {}).get("nodeId", 0)

            if node_id != 0:
                box_msg_id = client.send_command("DOM.getBoxModel", {"nodeId": node_id}, session_id=session_id)
                box_res = client.wait_for_response(box_msg_id)
                if "result" in box_res and "model" in box_res["result"]:
                    attr_msg_id = client.send_command("DOM.getAttributes", {"nodeId": node_id}, session_id=session_id)
                    attr_res = client.wait_for_response(attr_msg_id)
                    attrs = attr_res.get("result", {}).get("attributes", [])
                    is_disabled = False
                    for k in range(0, len(attrs) - 1, 2):
                        if attrs[k].lower() == "disabled":
                            is_disabled = True
                            break

                    if not is_disabled:
                        quad = box_res["result"]["model"]["border"]
                        return node_id, quad

        elapsed = time.monotonic() - start_time
        if elapsed >= timeout_s:
            return None, None

        sleep_s = min(backoff_ms / 1000.0, timeout_s - elapsed)
        if sleep_s > 0:
            time.sleep(sleep_s)

        backoff_ms = min(backoff_ms * 2, 64)


def perform_click(client, session_id, quad):
    xs = [quad[0], quad[2], quad[4], quad[6]]
    ys = [quad[1], quad[3], quad[5], quad[7]]
    cx = math.floor(sum(xs) / 4.0)
    cy = math.floor(sum(ys) / 4.0)

    p1 = {"type": "mouseMoved", "x": cx, "y": cy, "button": "none"}
    p2 = {"type": "mousePressed", "x": cx, "y": cy, "button": "left", "clickCount": 1}
    p3 = {"type": "mouseReleased", "x": cx, "y": cy, "button": "left", "clickCount": 1}

    m1 = client.send_command("Input.dispatchMouseEvent", p1, session_id=session_id)
    m2 = client.send_command("Input.dispatchMouseEvent", p2, session_id=session_id)
    m3 = client.send_command("Input.dispatchMouseEvent", p3, session_id=session_id)

    client.wait_for_response(m1)
    client.wait_for_response(m2)
    client.wait_for_response(m3)


def perform_type(client, session_id, node_id, text):
    f_msg = client.send_command("DOM.focus", {"nodeId": node_id}, session_id=session_id)
    client.wait_for_response(f_msg)

    t_msg = client.send_command("Input.insertText", {"text": text}, session_id=session_id)
    client.wait_for_response(t_msg)


def rAF_settle(client, session_id):
    expr = "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
    msg_id = client.send_command("Runtime.evaluate", {"expression": expr, "awaitPromise": True}, session_id=session_id)
    client.wait_for_response(msg_id)


def do_capture(client, session_id):
    rAF_settle(client, session_id)

    snap_id = client.send_command("DOMSnapshot.captureSnapshot", {"computedStyles": [], "includeDOMRects": True}, session_id=session_id)
    ax_id = client.send_command("Accessibility.getFullAXTree", session_id=session_id)

    snap_res = client.wait_for_response(snap_id)
    ax_res = client.wait_for_response(ax_id)

    nodes = parse_nodes_and_capture(snap_res.get("result", {}), ax_res.get("result", {}))
    return nodes


def main():
    parser = argparse.ArgumentParser(description="LANG-X X2 browser interaction tool")
    parser.add_argument("--page", required=True, help="Path to local HTML file")
    parser.add_argument("--steps", required=True, help="Path to steps JSON file")
    parser.add_argument("--timing", required=True, help="Path to output timing JSON file")
    parser.add_argument("--timeout-s", type=float, default=30.0, help="Overall timeout in seconds")

    args = parser.parse_args()

    overall_start = time.monotonic_ns()
    global_deadline = time.monotonic() + args.timeout_s

    chrome_bin = os.environ.get("CHROME_BIN")
    if not chrome_bin:
        sys.exit(3)

    page_abs_path = os.path.abspath(args.page)
    if not os.path.exists(page_abs_path):
        sys.exit(2)

    page_basename = os.path.basename(page_abs_path)

    try:
        with open(args.steps, "r", encoding="utf-8") as f:
            steps_data = json.load(f)
    except Exception:
        sys.exit(2)

    tmp_profile_dir = tempfile.mkdtemp(prefix="x2_profile_")

    client = PipeCDPClient(chrome_bin, tmp_profile_dir, global_deadline=global_deadline)

    launch_start = time.monotonic_ns()
    client.start()
    launch_ms = (time.monotonic_ns() - launch_start) / 1e6

    states = []
    capture_ms_list = []
    steps_ms_list = []
    attempts_list = []

    try:
        attach_start = time.monotonic_ns()
        t_id = client.send_command("Target.getTargets")
        t_res = client.wait_for_response(t_id)
        targets = t_res.get("result", {}).get("targetInfos", [])
        page_target = next((t for t in targets if t.get("type") == "page"), None)
        if not page_target:
            sys.exit(4)
        target_id = page_target["targetId"]

        att_id = client.send_command("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        att_res = client.wait_for_response(att_id)
        session_id = att_res.get("result", {}).get("sessionId")
        if not session_id:
            sys.exit(4)

        pe_id = client.send_command("Page.enable", session_id=session_id)
        client.wait_for_response(pe_id)
        attach_ms = (time.monotonic_ns() - attach_start) / 1e6

        load_start = time.monotonic_ns()
        file_url = "file://" + page_abs_path
        nav_id = client.send_command("Page.navigate", {"url": file_url}, session_id=session_id)
        client.wait_for_response(nav_id)
        client.wait_for_event("Page.loadEventFired", session_id=session_id)
        load_ms = (time.monotonic_ns() - load_start) / 1e6

        cap_start = time.monotonic_ns()
        initial_nodes = do_capture(client, session_id)
        cap_dur = (time.monotonic_ns() - cap_start) / 1e6
        capture_ms_list.append(round(cap_dur, 3))

        states.append({
            "error": None,
            "nodes": initial_nodes,
            "ok": True,
            "op": "load",
            "step": -1
        })

        steps = steps_data.get("steps", [])
        for idx, step_obj in enumerate(steps):
            step_start = time.monotonic_ns()
            op = step_obj.get("op")
            selector = step_obj.get("selector", "")
            text = step_obj.get("text", "")
            timeout_ms = step_obj.get("timeout_ms", 2000)

            node_id, quad = resolve_step_target(client, session_id, selector, timeout_ms)

            step_ok = True
            step_err = None

            if node_id is None:
                step_ok = False
                step_err = "timeout"
            else:
                if op == "click":
                    perform_click(client, session_id, quad)
                elif op == "type":
                    perform_type(client, session_id, node_id, text)
                elif op == "wait":
                    pass

            step_dur = (time.monotonic_ns() - step_start) / 1e6
            steps_ms_list.append(round(step_dur, 3))
            attempts_list.append(1)

            cap_start = time.monotonic_ns()
            step_nodes = do_capture(client, session_id)
            cap_dur = (time.monotonic_ns() - cap_start) / 1e6
            capture_ms_list.append(round(cap_dur, 3))

            states.append({
                "error": step_err,
                "nodes": step_nodes,
                "ok": step_ok,
                "op": op,
                "step": idx
            })

    finally:
        client.close()
        if os.path.exists(tmp_profile_dir):
            shutil.rmtree(tmp_profile_dir, ignore_errors=True)

    out_obj = {
        "page": page_basename,
        "schema": "vqa.x2-state/v1",
        "states": states,
        "viewport": [1280, 800]
    }

    stdout_str = canonical_dumps(out_obj)
    sys.stdout.write(stdout_str + "\n")
    sys.stdout.flush()

    where_val = os.environ.get("X2_WHERE", "unlabelled")
    timing_obj = {
        "attempts": attempts_list,
        "attach_ms": round(attach_ms, 3),
        "capture_ms": capture_ms_list,
        "lang": "python",
        "launch_ms": round(launch_ms, 3),
        "load_ms": round(load_ms, 3),
        "seed": "py_w3",
        "steps_ms": steps_ms_list,
        "where": where_val
    }

    try:
        with open(args.timing, "w", encoding="utf-8") as f:
            f.write(canonical_dumps(timing_obj) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
