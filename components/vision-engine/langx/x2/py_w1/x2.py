#!/usr/bin/env python3
"""
LANG-X X2 browser seed, Python (py_w1).
Command-line tool for web page interaction via Chrome DevTools Protocol over pipes.
"""

import sys
import os
import json
import math
import time
import shutil
import tempfile
import argparse
import selectors
import subprocess

SKIP_TAGS = {"html", "head", "script", "style", "meta", "link", "title", "noscript"}

ACT_TAGS = {"a", "button", "select", "textarea", "summary", "input"}
ACT_ROLES = {
    "button", "link", "checkbox", "radio", "textbox", "searchbox",
    "combobox", "menuitem", "tab", "switch", "option", "slider", "spinbutton"
}


def canonical_dumps(obj):
    """
    Canonical serialization according to rule shared with host.
    Produces json with sorted keys, compact separators (',', ':'),
    non-ASCII escaped as \\uXXXX, control chars escaped, slashes unescaped.
    """
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def compute_box(x, y, w, h):
    """
    Compute closed integer box [x0, y0, x1, y1] or None if width/height is zero or non-positive.
    x0 = floor(x), y0 = floor(y)
    x1 = max(x0, ceil(x + w - 1))
    y1 = max(y0, ceil(y + h - 1))
    """
    if w <= 0 or h <= 0:
        return None
    x0 = math.floor(x)
    y0 = math.floor(y)
    x1 = max(x0, math.ceil(x + w - 1))
    y1 = max(y0, math.ceil(y + h - 1))
    return [x0, y0, x1, y1]


def collapse_and_truncate_name(name):
    if not name:
        return ""
    # Collapse runs of whitespace to one space, strip, cut to 120 code points
    collapsed = " ".join(name.split())
    return collapsed[:120]


def get_backoff_ms(step_index):
    """
    Rule R8 bounded backoff: 1, 2, 4, 8, 16, 32, 64, 64... ms
    """
    if step_index < 0:
        return 1
    if step_index < 6:
        return 1 << step_index
    return 64


class PipeFramingReader:
    def __init__(self, fd):
        self.fd = fd
        self.buf = bytearray()
        self.sel = selectors.DefaultSelector()
        self.sel.register(fd, selectors.EVENT_READ)

    def read_message(self, timeout=None):
        start = time.monotonic()
        while True:
            if b'\x00' in self.buf:
                idx = self.buf.index(b'\x00')
                msg_bytes = self.buf[:idx]
                self.buf = self.buf[idx + 1:]
                try:
                    return json.loads(msg_bytes.decode('utf-8'))
                except Exception:
                    sys.exit(7)

            if timeout is not None:
                elapsed = time.monotonic() - start
                remaining = max(0.0, timeout - elapsed)
                events = self.sel.select(timeout=remaining)
                if not events:
                    return None
            else:
                events = self.sel.select(timeout=0.5)

            if events:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    chunk = b''

                if not chunk:
                    # EOF reached
                    if len(self.buf) > 0:
                        # Rule R2 framing: torn tail at EOF
                        sys.exit(6)
                    return None
                self.buf.extend(chunk)


class CDPClient:
    def __init__(self, write_fd, read_fd):
        self.write_fd = write_fd
        self.reader = PipeFramingReader(read_fd)
        self.req_id = 0
        self.pending_events = []

    def send_request(self, method, params=None, session_id=None):
        self.req_id += 1
        curr_id = self.req_id
        req = {"id": curr_id, "method": method}
        if params is not None:
            req["params"] = params
        if session_id is not None:
            req["sessionId"] = session_id
        payload = json.dumps(req).encode('utf-8') + b'\x00'
        os.write(self.write_fd, payload)
        return curr_id

    def call(self, method, params=None, session_id=None, timeout=30.0, overall_deadline=None):
        if overall_deadline is not None and time.monotonic() >= overall_deadline:
            sys.exit(5)

        req_id = self.send_request(method, params, session_id)
        start = time.monotonic()
        while True:
            if overall_deadline is not None and time.monotonic() >= overall_deadline:
                sys.exit(5)

            elapsed = time.monotonic() - start
            if elapsed > timeout:
                return None

            rem = timeout - elapsed
            if overall_deadline is not None:
                rem_overall = overall_deadline - time.monotonic()
                if rem_overall <= 0:
                    sys.exit(5)
                rem = min(rem, rem_overall)

            msg = self.reader.read_message(timeout=max(0.05, rem))
            if msg is None:
                if overall_deadline is not None and time.monotonic() >= overall_deadline:
                    sys.exit(5)
                if elapsed >= timeout:
                    return None
                continue

            # Validate message (Rule R14)
            if "id" in msg:
                msg_id = msg["id"]
                if not isinstance(msg_id, int):
                    sys.exit(7)
                if msg_id == req_id:
                    if "result" in msg:
                        return msg["result"]
                    elif "error" in msg:
                        return {"error": msg["error"]}
                    else:
                        sys.exit(7)
                else:
                    sys.exit(7)
            elif "method" in msg and "id" not in msg:
                self.pending_events.append(msg)
            else:
                sys.exit(7)

    def wait_for_event(self, method, session_id=None, timeout=30.0, overall_deadline=None):
        if overall_deadline is not None and time.monotonic() >= overall_deadline:
            sys.exit(5)

        start = time.monotonic()
        while True:
            if overall_deadline is not None and time.monotonic() >= overall_deadline:
                sys.exit(5)

            for i, ev in enumerate(self.pending_events):
                if ev.get("method") == method and (session_id is None or ev.get("sessionId") == session_id):
                    return self.pending_events.pop(i)

            elapsed = time.monotonic() - start
            if elapsed > timeout:
                return None

            rem = timeout - elapsed
            if overall_deadline is not None:
                rem_overall = overall_deadline - time.monotonic()
                if rem_overall <= 0:
                    sys.exit(5)
                rem = min(rem, rem_overall)

            msg = self.reader.read_message(timeout=max(0.05, rem))
            if msg is None:
                if overall_deadline is not None and time.monotonic() >= overall_deadline:
                    sys.exit(5)
                if elapsed >= timeout:
                    return None
                continue

            if "id" in msg:
                sys.exit(7)
            elif "method" in msg and "id" not in msg:
                if msg["method"] == method and (session_id is None or msg.get("sessionId") == session_id):
                    return msg
                else:
                    self.pending_events.append(msg)
            else:
                sys.exit(7)


def build_nodes_list(snap_doc, strings, ax_nodes):
    nodes_data = snap_doc['nodes']
    layout_data = snap_doc['layout']

    ax_map = {}
    for ax in ax_nodes:
        if "backendDOMNodeId" in ax:
            ax_map[ax["backendDOMNodeId"]] = ax

    layout_boxes = {}
    node_indices = layout_data.get('nodeIndex', [])
    bounds_list = layout_data.get('bounds', [])

    for idx, node_idx in enumerate(node_indices):
        bounds = bounds_list[idx] if idx < len(bounds_list) else None
        if bounds and len(bounds) == 4:
            x, y, w, h = bounds
            box = compute_box(x, y, w, h)
            b_id = nodes_data['backendNodeId'][node_idx]
            layout_boxes[b_id] = box

    input_value_map = {}
    if 'inputValue' in nodes_data and 'index' in nodes_data['inputValue']:
        iv_indices = nodes_data['inputValue']['index']
        iv_values = nodes_data['inputValue']['value']
        for idx_pos, node_i in enumerate(iv_indices):
            str_i = iv_values[idx_pos]
            input_value_map[node_i] = strings[str_i]

    input_checked_map = {}
    if 'inputChecked' in nodes_data and 'index' in nodes_data['inputChecked']:
        ic_indices = nodes_data['inputChecked']['index']
        for node_i in ic_indices:
            input_checked_map[node_i] = True

    option_selected_map = {}
    if 'optionSelected' in nodes_data and 'index' in nodes_data['optionSelected']:
        os_indices = nodes_data['optionSelected']['index']
        for node_i in os_indices:
            option_selected_map[node_i] = True

    emitted_records = []
    num_nodes = len(nodes_data['nodeName'])

    for i in range(num_nodes):
        backend_id = nodes_data['backendNodeId'][i]

        if backend_id not in layout_boxes:
            continue

        tag_name = strings[nodes_data['nodeName'][i]].lower()
        if tag_name in SKIP_TAGS:
            continue

        box = layout_boxes[backend_id]

        attrs = {}
        if 'attributes' in nodes_data and i < len(nodes_data['attributes']):
            attrs_raw = nodes_data['attributes'][i]
            if attrs_raw:
                for k in range(0, len(attrs_raw), 2):
                    attrs[strings[attrs_raw[k]].lower()] = strings[attrs_raw[k + 1]]

        elem_id = attrs.get('id', '')
        is_disabled = ('disabled' in attrs)

        ax_info = ax_map.get(backend_id)
        role = ""
        name = ""
        if ax_info and not ax_info.get("ignored", False):
            if "role" in ax_info and "value" in ax_info["role"]:
                role = ax_info["role"]["value"]
            if "name" in ax_info and "value" in ax_info["name"]:
                name = ax_info["name"]["value"]

        name = collapse_and_truncate_name(name)

        value = ""
        if tag_name in ("input", "textarea", "select"):
            if tag_name == "select":
                val_candidate = input_value_map.get(i, "")
                value = val_candidate if val_candidate else ""
            else:
                value = input_value_map.get(i, "")

        checked = False
        if tag_name == "input":
            input_type = attrs.get("type", "text").lower()
            if input_type in ("checkbox", "radio"):
                checked = input_checked_map.get(i, False)

        act = False
        if box is not None and not is_disabled:
            input_type = attrs.get("type", "").lower()
            is_act_tag = False
            if tag_name == "input" and input_type != "hidden":
                is_act_tag = True
            elif tag_name == "a" and "href" in attrs:
                is_act_tag = True
            elif tag_name in ("button", "select", "textarea", "summary"):
                is_act_tag = True

            is_act_role = (role in ACT_ROLES)
            if is_act_tag or is_act_role:
                act = True

        rec = {
            "act": act,
            "box": box,
            "checked": checked,
            "i": len(emitted_records),
            "id": elem_id,
            "name": name,
            "role": role,
            "tag": tag_name,
            "value": value
        }
        emitted_records.append(rec)

    return emitted_records


def settle_page(cdp, session_id, overall_deadline=None):
    expr = "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
    cdp.call("Runtime.evaluate", {
        "expression": expr,
        "awaitPromise": True
    }, session_id=session_id, overall_deadline=overall_deadline)


def capture_state(cdp, session_id, step_idx, op, ok, error, overall_deadline=None):
    settle_page(cdp, session_id, overall_deadline=overall_deadline)

    snap_res = cdp.call("DOMSnapshot.captureSnapshot", {
        "computedStyles": [],
        "includeDOMRects": True
    }, session_id=session_id, overall_deadline=overall_deadline)

    ax_res = cdp.call("Accessibility.getFullAXTree", session_id=session_id, overall_deadline=overall_deadline)

    if not snap_res or "documents" not in snap_res or not snap_res["documents"]:
        nodes = []
    else:
        doc = snap_res["documents"][0]
        strings = snap_res.get("strings", [])
        ax_nodes = ax_res.get("nodes", []) if ax_res else []
        nodes = build_nodes_list(doc, strings, ax_nodes)

    return {
        "error": error,
        "nodes": nodes,
        "op": op,
        "ok": ok,
        "step": step_idx
    }


def resolve_step_target(cdp, session_id, selector, timeout_ms, overall_deadline=None):
    start_t = time.monotonic()
    timeout_s = timeout_ms / 1000.0
    backoff_idx = 0

    while True:
        if overall_deadline is not None and time.monotonic() >= overall_deadline:
            sys.exit(5)

        doc_res = cdp.call("DOM.getDocument", {"depth": 1}, session_id=session_id, overall_deadline=overall_deadline)
        if doc_res and "root" in doc_res:
            root_node_id = doc_res["root"]["nodeId"]
            q_res = cdp.call("DOM.querySelector", {
                "nodeId": root_node_id,
                "selector": selector
            }, session_id=session_id, overall_deadline=overall_deadline)

            if q_res and "nodeId" in q_res:
                node_id = q_res["nodeId"]
                if node_id != 0:
                    box_res = cdp.call("DOM.getBoxModel", {"nodeId": node_id}, session_id=session_id, overall_deadline=overall_deadline)
                    if box_res and "model" in box_res:
                        attr_res = cdp.call("DOM.getAttributes", {"nodeId": node_id}, session_id=session_id, overall_deadline=overall_deadline)
                        is_disabled = False
                        if attr_res and "attributes" in attr_res:
                            attrs = attr_res["attributes"]
                            for k in range(0, len(attrs), 2):
                                if attrs[k].lower() == "disabled":
                                    is_disabled = True
                                    break
                        if not is_disabled:
                            return node_id, box_res["model"]

        elapsed = time.monotonic() - start_t
        if elapsed >= timeout_s:
            return 0, None

        sleep_ms = get_backoff_ms(backoff_idx)
        backoff_idx += 1
        sleep_s = sleep_ms / 1000.0

        if overall_deadline is not None:
            rem_overall = overall_deadline - time.monotonic()
            if rem_overall <= 0:
                sys.exit(5)
            sleep_s = min(sleep_s, rem_overall)

        time.sleep(sleep_s)


def execute_step(cdp, session_id, step_obj, overall_deadline=None):
    op = step_obj.get("op")
    selector = step_obj.get("selector", "")
    text = step_obj.get("text", "")
    timeout_ms = step_obj.get("timeout_ms", 2000)

    node_id, box_model = resolve_step_target(cdp, session_id, selector, timeout_ms, overall_deadline=overall_deadline)
    if node_id == 0 or box_model is None:
        return False, "timeout"

    if op == "click":
        quad = box_model.get("border", [])
        if len(quad) < 8:
            return False, "timeout"
        x_avg = sum(quad[0::2]) / 4.0
        y_avg = sum(quad[1::2]) / 4.0
        cx = math.floor(x_avg)
        cy = math.floor(y_avg)

        cdp.call("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": cx, "y": cy, "button": "none"
        }, session_id=session_id, overall_deadline=overall_deadline)
        cdp.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": cx, "y": cy, "button": "left", "clickCount": 1
        }, session_id=session_id, overall_deadline=overall_deadline)
        cdp.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": cx, "y": cy, "button": "left", "clickCount": 1
        }, session_id=session_id, overall_deadline=overall_deadline)
        return True, None

    elif op == "type":
        cdp.call("DOM.focus", {"nodeId": node_id}, session_id=session_id, overall_deadline=overall_deadline)
        cdp.call("Input.insertText", {"text": text}, session_id=session_id, overall_deadline=overall_deadline)
        return True, None

    elif op == "wait":
        return True, None

    return False, "invalid op"


def main():
    parser = argparse.ArgumentParser(description="LANG-X X2 browser interaction tool")
    parser.add_argument("--page", required=True, help="PATH to local HTML file")
    parser.add_argument("--steps", required=True, help="PATH to steps JSON file")
    parser.add_argument("--timing", required=True, help="PATH for timing file")
    parser.add_argument("--timeout-s", type=float, default=30.0, help="Overall timeout in seconds")

    try:
        args = parser.parse_args()
    except SystemExit:
        sys.exit(2)

    chrome_bin = os.environ.get("CHROME_BIN")
    if not chrome_bin:
        sys.exit(3)

    if not os.path.exists(args.page) or not os.path.exists(args.steps):
        sys.exit(2)

    try:
        with open(args.steps, 'r', encoding='utf-8') as f:
            steps_data = json.load(f)
        steps_list = steps_data.get("steps", [])
    except Exception:
        sys.exit(2)

    abs_page_path = os.path.abspath(args.page)
    page_basename = os.path.basename(abs_page_path)

    where_val = os.environ.get("X2_WHERE", "unlabelled")
    t0 = time.monotonic_ns()
    overall_deadline = time.monotonic() + args.timeout_s

    r1, w1 = os.pipe()
    r2, w2 = os.pipe()

    tmp_dir = tempfile.mkdtemp(prefix="x2_profile_")

    cmd = [
        chrome_bin,
        "--headless=new",
        "--remote-debugging-pipe",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-extensions",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--window-size=1280,800",
        f"--user-data-dir={tmp_dir}",
        "about:blank"
    ]

    def preexec():
        os.dup2(r1, 3)
        os.dup2(w2, 4)

    try:
        proc = subprocess.Popen(
            cmd,
            pass_fds=(3, 4),
            preexec_fn=preexec,
            start_new_session=True
        )
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.exit(3)

    os.close(r1)
    os.close(w2)

    cdp = CDPClient(w1, r2)
    launch_ms = (time.monotonic_ns() - t0) / 1e6

    # Attach to target
    t_attach_start = time.monotonic_ns()
    targets_res = cdp.call("Target.getTargets", timeout=args.timeout_s, overall_deadline=overall_deadline)
    if not targets_res or "targetInfos" not in targets_res:
        _cleanup_proc(proc, tmp_dir)
        sys.exit(4)

    page_target = None
    for t in targets_res["targetInfos"]:
        if t.get("type") == "page":
            page_target = t
            break

    if not page_target:
        _cleanup_proc(proc, tmp_dir)
        sys.exit(4)

    attach_res = cdp.call("Target.attachToTarget", {
        "targetId": page_target["targetId"],
        "flatten": True
    }, timeout=args.timeout_s, overall_deadline=overall_deadline)

    if not attach_res or "sessionId" not in attach_res:
        _cleanup_proc(proc, tmp_dir)
        sys.exit(4)

    session_id = attach_res["sessionId"]
    attach_ms = (time.monotonic_ns() - t_attach_start) / 1e6

    # Enable Page and navigate
    t_load_start = time.monotonic_ns()
    cdp.call("Page.enable", session_id=session_id, timeout=args.timeout_s, overall_deadline=overall_deadline)

    file_url = f"file://{abs_page_path}"
    cdp.call("Page.navigate", {"url": file_url}, session_id=session_id, timeout=args.timeout_s, overall_deadline=overall_deadline)

    load_ev = cdp.wait_for_event("Page.loadEventFired", session_id=session_id, timeout=args.timeout_s, overall_deadline=overall_deadline)
    if not load_ev:
        _cleanup_proc(proc, tmp_dir)
        sys.exit(4)

    load_ms = (time.monotonic_ns() - t_load_start) / 1e6

    capture_ms_list = []
    steps_ms_list = []
    attempts_list = []

    states_list = []

    # Initial capture (step -1, op load)
    t_cap_start = time.monotonic_ns()
    init_state = capture_state(cdp, session_id, -1, "load", True, None, overall_deadline=overall_deadline)
    capture_ms_list.append(round((time.monotonic_ns() - t_cap_start) / 1e6, 3))
    states_list.append(init_state)

    # Process steps
    for step_idx, step_obj in enumerate(steps_list):
        t_step_start = time.monotonic_ns()
        ok, err_str = execute_step(cdp, session_id, step_obj, overall_deadline=overall_deadline)
        steps_ms_list.append(round((time.monotonic_ns() - t_step_start) / 1e6, 3))
        attempts_list.append(1)

        t_cap_start = time.monotonic_ns()
        st = capture_state(cdp, session_id, step_idx, step_obj.get("op"), ok, err_str, overall_deadline=overall_deadline)
        capture_ms_list.append(round((time.monotonic_ns() - t_cap_start) / 1e6, 3))
        states_list.append(st)

    out_obj = {
        "page": page_basename,
        "schema": "vqa.x2-state/v1",
        "states": states_list,
        "viewport": [1280, 800]
    }

    sys.stdout.write(canonical_dumps(out_obj) + "\n")
    sys.stdout.flush()

    timing_obj = {
        "attempts": attempts_list,
        "attach_ms": round(attach_ms, 3),
        "capture_ms": capture_ms_list,
        "lang": "python",
        "launch_ms": round(launch_ms, 3),
        "load_ms": round(load_ms, 3),
        "seed": "py_w1",
        "steps_ms": steps_ms_list,
        "where": where_val
    }

    try:
        with open(args.timing, "w", encoding="utf-8") as f:
            f.write(canonical_dumps(timing_obj) + "\n")
    except Exception:
        pass

    _shutdown_browser(cdp, proc, tmp_dir)
    sys.exit(0)


def _cleanup_proc(proc, tmp_dir):
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass
    shutil.rmtree(tmp_dir, ignore_errors=True)


def _shutdown_browser(cdp, proc, tmp_dir):
    try:
        cdp.call("Browser.close", timeout=2.0)
        proc.wait(timeout=2.0)
    except Exception:
        _cleanup_proc(proc, tmp_dir)
        return
    shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
