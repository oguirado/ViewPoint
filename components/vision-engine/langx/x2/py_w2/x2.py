#!/usr/bin/env python3
import argparse
import json
import math
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path


def canonical_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


class ProtocolError(Exception):
    pass


class CDPClient:
    def __init__(self, proc, p_write_fd, p_read_fd, deadline_t=None):
        self.proc = proc
        self.p_write_fd = p_write_fd
        self.p_read_fd = p_read_fd
        self.deadline_t = deadline_t
        self.read_buf = bytearray()
        self.next_id = 1
        self.pending_responses = {}
        self.event_queue = []

    def _check_deadline(self):
        if self.deadline_t is not None and time.monotonic() > self.deadline_t:
            sys.exit(5)

    def _read_from_pipe(self, timeout_s=0.1):
        self._check_deadline()
        r, _, _ = select.select([self.p_read_fd], [], [], timeout_s)
        if not r:
            self._check_deadline()
            return
        try:
            chunk = os.read(self.p_read_fd, 65536)
        except OSError:
            chunk = b""

        if not chunk:
            # EOF reached
            if len(self.read_buf) > 0:
                # Torn tail!
                sys.exit(6)
            return

        self.read_buf.extend(chunk)

        while b"\x00" in self.read_buf:
            idx = self.read_buf.index(b"\x00")
            msg_bytes = bytes(self.read_buf[:idx])
            del self.read_buf[: idx + 1]

            try:
                msg_str = msg_bytes.decode('utf-8')
                msg = json.loads(msg_str)
            except Exception:
                sys.exit(7)

            if not isinstance(msg, dict):
                sys.exit(7)

            if "id" in msg:
                req_id = msg["id"]
                if not isinstance(req_id, int) or isinstance(req_id, bool):
                    sys.exit(7)
                if req_id not in self.pending_responses:
                    # Unknown response ID
                    sys.exit(7)
                if "result" not in msg and "error" not in msg:
                    sys.exit(7)
                self.pending_responses[req_id] = msg
            elif "method" in msg:
                if not isinstance(msg["method"], str):
                    sys.exit(7)
                if len(self.event_queue) < 1000:
                    self.event_queue.append(msg)
            else:
                sys.exit(7)

    def send_command(self, method, params=None, session_id=None, timeout_s=10.0):
        self._check_deadline()
        req_id = self.next_id
        self.next_id += 1

        req = {"id": req_id, "method": method}
        if params is not None:
            req["params"] = params
        if session_id is not None:
            req["sessionId"] = session_id

        self.pending_responses[req_id] = None

        raw = json.dumps(req, ensure_ascii=True).encode('utf-8') + b"\x00"
        try:
            os.write(self.p_write_fd, raw)
        except OSError:
            sys.exit(3)

        start = time.monotonic()
        while self.pending_responses[req_id] is None:
            self._check_deadline()
            if time.monotonic() - start > timeout_s:
                raise TimeoutError(f"Command {method} timed out after {timeout_s}s")
            if self.proc.poll() is not None and not self.read_buf:
                if len(self.read_buf) > 0:
                    sys.exit(6)
                sys.exit(3)
            self._read_from_pipe(timeout_s=0.05)

        res = self.pending_responses.pop(req_id)
        if "error" in res:
            raise ProtocolError(res["error"])
        return res.get("result", {})

    def wait_for_event(self, method, session_id=None, timeout_s=10.0):
        start = time.monotonic()
        while True:
            self._check_deadline()
            for i, evt in enumerate(self.event_queue):
                if evt.get("method") == method:
                    if session_id is None or evt.get("sessionId") == session_id:
                        return self.event_queue.pop(i)
            if time.monotonic() - start > timeout_s:
                raise TimeoutError(f"Event {method} timed out after {timeout_s}s")
            self._read_from_pipe(timeout_s=0.05)


def parse_box(x, y, w, h):
    if w <= 0 or h <= 0:
        return None
    x0 = math.floor(x)
    y0 = math.floor(y)
    x1 = max(x0, math.ceil(x + w - 1))
    y1 = max(y0, math.ceil(y + h - 1))
    return [int(x0), int(y0), int(x1), int(y1)]


def is_actionable(box, attrs, tag, role):
    if box is None:
        return False
    if "disabled" in attrs:
        return False

    tag = tag.lower() if tag else ""
    role = role.lower() if role else ""

    act_tags = {"button", "select", "textarea", "summary"}
    act_roles = {
        "button",
        "link",
        "checkbox",
        "radio",
        "textbox",
        "searchbox",
        "combobox",
        "menuitem",
        "tab",
        "switch",
        "option",
        "slider",
        "spinbutton",
    }

    if tag == "a" and "href" in attrs:
        return True
    if tag == "input" and attrs.get("type", "").lower() != "hidden":
        return True
    if tag in act_tags:
        return True
    if role in act_roles:
        return True

    return False


def extract_node_records(snapshot_res, ax_res):
    documents = snapshot_res.get("documents", [])
    if not documents:
        return []

    main_doc = documents[0]
    nodes = main_doc.get("nodes", {})
    layout = main_doc.get("layout", {})
    strings = snapshot_res.get("strings", [])

    def get_str(idx):
        if 0 <= idx < len(strings):
            return strings[idx]
        return ""

    layout_node_indices = layout.get("nodeIndex", [])
    layout_bounds = layout.get("bounds", [])
    layout_map = {}
    for l_idx, n_idx in enumerate(layout_node_indices):
        if l_idx < len(layout_bounds):
            layout_map[n_idx] = layout_bounds[l_idx]

    ax_map = {}
    if ax_res and "nodes" in ax_res:
        for ax_node in ax_res["nodes"]:
            b_id = ax_node.get("backendDOMNodeId")
            if b_id is not None:
                ax_map[b_id] = ax_node

    def get_rare_string(rare_obj, n_idx):
        if not rare_obj or not isinstance(rare_obj, dict):
            return None
        indices = rare_obj.get("index", [])
        values = rare_obj.get("value", [])
        if n_idx in indices:
            pos = indices.index(n_idx)
            if pos < len(values):
                return get_str(values[pos])
        return None

    node_types = nodes.get("nodeType", [])
    node_names = nodes.get("nodeName", [])
    backend_ids = nodes.get("backendNodeId", [])
    attributes_list = nodes.get("attributes", [])
    input_values = nodes.get("inputValue", {})

    skip_tags = {"html", "head", "script", "style", "meta", "link", "title", "noscript"}

    emitted_records = []
    emitted_counter = 0

    num_nodes = len(node_types)
    for n_idx in range(num_nodes):
        if node_types[n_idx] != 1:  # Element node
            continue

        tag = get_str(node_names[n_idx]).lower()
        if tag in skip_tags:
            continue

        if n_idx not in layout_map:
            continue

        bounds = layout_map[n_idx]
        x, y, w, h = bounds[0], bounds[1], bounds[2], bounds[3]
        box = parse_box(x, y, w, h)

        attrs = {}
        if n_idx < len(attributes_list):
            attr_pairs = attributes_list[n_idx]
            for k_idx, v_idx in zip(attr_pairs[0::2], attr_pairs[1::2]):
                attrs[get_str(k_idx).lower()] = get_str(v_idx)

        element_id = attrs.get("id", "")

        b_id = backend_ids[n_idx] if n_idx < len(backend_ids) else None
        ax_node = ax_map.get(b_id)

        role = ""
        name = ""
        if ax_node and not ax_node.get("ignored", False):
            r_val = ax_node.get("role", {})
            if isinstance(r_val, dict):
                role = str(r_val.get("value", ""))
            elif isinstance(r_val, str):
                role = r_val

            n_val = ax_node.get("name", {})
            raw_name = ""
            if isinstance(n_val, dict):
                raw_name = str(n_val.get("value", ""))
            elif isinstance(n_val, str):
                raw_name = n_val

            if raw_name:
                collapsed = re.sub(r'\s+', ' ', raw_name).strip()
                name = collapsed[:120]

        value = ""
        if tag in {"input", "textarea", "select"}:
            rare_val = get_rare_string(input_values, n_idx)
            if rare_val is not None:
                value = rare_val
            elif "value" in attrs:
                value = attrs["value"]
            elif ax_node and "value" in ax_node:
                v_obj = ax_node["value"]
                if isinstance(v_obj, dict):
                    value = str(v_obj.get("value", ""))

        checked = False
        if tag == "input" and attrs.get("type", "").lower() in {"checkbox", "radio"}:
            if "checked" in attrs:
                checked = True

        act = is_actionable(box, attrs, tag, role)

        rec = {
            "act": act,
            "box": box,
            "checked": checked,
            "i": emitted_counter,
            "id": element_id,
            "name": name,
            "role": role,
            "tag": tag,
            "value": value,
        }
        emitted_records.append(rec)
        emitted_counter += 1

    return emitted_records


def compute_backoff_delays():
    return [1, 2, 4, 8, 16, 32, 64]


def main():
    start_total_t = time.monotonic()

    parser = argparse.ArgumentParser(description="LANG-X X2 Browser Seed")
    parser.add_argument("--page", required=True, help="Path to local HTML file")
    parser.add_argument("--steps", required=True, help="Path to steps JSON file")
    parser.add_argument("--timing", required=True, help="Path to timing JSON file")
    parser.add_argument("--timeout-s", type=float, default=30.0, help="Timeout in seconds")

    try:
        args = parser.parse_args()
    except SystemExit:
        sys.exit(2)

    deadline_t = start_total_t + args.timeout_s

    chrome_bin = os.environ.get("CHROME_BIN")
    if not chrome_bin or not os.path.isfile(chrome_bin) or not os.access(chrome_bin, os.X_OK):
        sys.exit(3)

    page_path = os.path.abspath(args.page)
    if not os.path.exists(page_path):
        sys.exit(2)

    steps_path = os.path.abspath(args.steps)
    if not os.path.exists(steps_path):
        sys.exit(2)

    try:
        with open(steps_path, "r", encoding="utf-8") as f:
            steps_data = json.load(f)
        steps = steps_data.get("steps", [])
    except Exception:
        sys.exit(2)

    user_data_dir = tempfile.mkdtemp(prefix="x2_chrome_")

    p1_r, p1_w = os.pipe()
    p2_r, p2_w = os.pipe()

    if p1_w in (3, 4):
        p1_w = os.dup(p1_w)
    if p2_r in (3, 4):
        p2_r = os.dup(p2_r)

    if p1_r != 3:
        os.dup2(p1_r, 3)
        os.close(p1_r)

    if p2_w != 4:
        os.dup2(p2_w, 4)
        os.close(p2_w)

    parent_write_fd = p1_w
    parent_read_fd = p2_r

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
        f"--user-data-dir={user_data_dir}",
        "about:blank",
    ]

    t0 = time.monotonic_ns()
    try:
        proc = subprocess.Popen(
            cmd,
            pass_fds=(3, 4),
            preexec_fn=os.setsid,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        shutil.rmtree(user_data_dir, ignore_errors=True)
        sys.exit(3)

    os.close(3)
    os.close(4)

    client = CDPClient(proc, parent_write_fd, parent_read_fd, deadline_t=deadline_t)
    launch_ms = (time.monotonic_ns() - t0) / 1e6

    try:
        t_attach = time.monotonic_ns()
        try:
            targets_res = client.send_command("Target.getTargets")
        except TimeoutError:
            sys.exit(5)

        targets = targets_res.get("targetInfos", [])
        page_target = None
        for t in targets:
            if t.get("type") == "page":
                page_target = t
                break

        if not page_target:
            sys.exit(4)

        target_id = page_target["targetId"]
        try:
            attach_res = client.send_command(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )
        except TimeoutError:
            sys.exit(5)

        session_id = attach_res.get("sessionId")
        attach_ms = (time.monotonic_ns() - t_attach) / 1e6

        t_load = time.monotonic_ns()
        try:
            client.send_command("Page.enable", session_id=session_id)
            file_url = Path(page_path).as_uri()
            client.send_command("Page.navigate", {"url": file_url}, session_id=session_id)
            client.wait_for_event("Page.loadEventFired", session_id=session_id, timeout_s=15.0)
        except TimeoutError:
            if time.monotonic() > deadline_t:
                sys.exit(5)
            sys.exit(4)

        load_ms = (time.monotonic_ns() - t_load) / 1e6

        def settle():
            if time.monotonic() > deadline_t:
                sys.exit(5)
            try:
                client.send_command(
                    "Runtime.evaluate",
                    {
                        "expression": "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))",
                        "awaitPromise": True,
                    },
                    session_id=session_id,
                )
            except TimeoutError:
                sys.exit(5)

        def do_capture():
            settle()
            try:
                snapshot_res = client.send_command(
                    "DOMSnapshot.captureSnapshot",
                    {"computedStyles": [], "includeDOMRects": True},
                    session_id=session_id,
                )
                ax_res = client.send_command(
                    "Accessibility.getFullAXTree", session_id=session_id
                )
            except TimeoutError:
                sys.exit(5)
            return extract_node_records(snapshot_res, ax_res)

        capture_ms_list = []
        steps_ms_list = []
        states = []

        # Initial capture
        t_cap0 = time.monotonic_ns()
        nodes0 = do_capture()
        cap0_ms = (time.monotonic_ns() - t_cap0) / 1e6
        capture_ms_list.append(cap0_ms)

        states.append({
            "error": None,
            "nodes": nodes0,
            "ok": True,
            "op": "load",
            "step": -1,
        })

        backoff_delays = compute_backoff_delays()

        # Run steps
        for step_idx, step in enumerate(steps):
            if time.monotonic() > deadline_t:
                sys.exit(5)

            t_step = time.monotonic_ns()
            op = step.get("op")
            selector = step.get("selector", "")
            text = step.get("text", "")
            timeout_ms = float(step.get("timeout_ms", 2000))

            start_t = time.monotonic()
            backoff_idx = 0
            ready = False
            target_node_id = 0
            center_point = None

            while (time.monotonic() - start_t) * 1000.0 < timeout_ms:
                if time.monotonic() > deadline_t:
                    sys.exit(5)

                try:
                    doc_res = client.send_command("DOM.getDocument", {"depth": 1}, session_id=session_id)
                    root_id = doc_res.get("root", {}).get("nodeId", 0)
                    if root_id != 0:
                        qs_res = client.send_command(
                            "DOM.querySelector", {"nodeId": root_id, "selector": selector}, session_id=session_id
                        )
                        target_node_id = qs_res.get("nodeId", 0)
                        if target_node_id != 0:
                            box_res = client.send_command(
                                "DOM.getBoxModel", {"nodeId": target_node_id}, session_id=session_id
                            )
                            model = box_res.get("model", {})
                            border = model.get("border", [])
                            attrs_res = client.send_command(
                                "DOM.getAttributes", {"nodeId": target_node_id}, session_id=session_id
                            )
                            attr_list = attrs_res.get("attributes", [])
                            attrs_dict = {
                                attr_list[i].lower(): attr_list[i + 1]
                                for i in range(0, len(attr_list), 2)
                            }

                            if "disabled" not in attrs_dict and len(border) >= 8:
                                ready = True
                                xs = border[0::2]
                                ys = border[1::2]
                                mean_x = sum(xs) / len(xs)
                                mean_y = sum(ys) / len(ys)
                                center_point = (math.floor(mean_x), math.floor(mean_y))
                                break
                except Exception:
                    pass

                delay_ms = backoff_delays[min(backoff_idx, len(backoff_delays) - 1)]
                backoff_idx += 1
                rem_ms = timeout_ms - ((time.monotonic() - start_t) * 1000.0)
                if rem_ms <= 0:
                    break
                time.sleep(min(delay_ms, rem_ms) / 1000.0)

            ok = True
            err_str = None

            if not ready:
                ok = False
                err_str = "timeout"
            else:
                try:
                    if op == "click":
                        cx, cy = center_point
                        client.send_command(
                            "Input.dispatchMouseEvent",
                            {"type": "mouseMoved", "x": cx, "y": cy, "button": "none"},
                            session_id=session_id,
                        )
                        client.send_command(
                            "Input.dispatchMouseEvent",
                            {
                                "type": "mousePressed",
                                "x": cx,
                                "y": cy,
                                "button": "left",
                                "clickCount": 1,
                            },
                            session_id=session_id,
                        )
                        client.send_command(
                            "Input.dispatchMouseEvent",
                            {
                                "type": "mouseReleased",
                                "x": cx,
                                "y": cy,
                                "button": "left",
                                "clickCount": 1,
                            },
                            session_id=session_id,
                        )
                    elif op == "type":
                        client.send_command(
                            "DOM.focus", {"nodeId": target_node_id}, session_id=session_id
                        )
                        client.send_command(
                            "Input.insertText", {"text": text}, session_id=session_id
                        )
                    elif op == "wait":
                        pass
                except TimeoutError:
                    sys.exit(5)

            step_ms = (time.monotonic_ns() - t_step) / 1e6
            steps_ms_list.append(step_ms)

            # Settle & capture after step
            t_cap_step = time.monotonic_ns()
            step_nodes = do_capture()
            cap_step_ms = (time.monotonic_ns() - t_cap_step) / 1e6
            capture_ms_list.append(cap_step_ms)

            states.append({
                "error": err_str,
                "nodes": step_nodes,
                "ok": ok,
                "op": op,
                "step": step_idx,
            })

        out_obj = {
            "page": os.path.basename(page_path),
            "schema": "vqa.x2-state/v1",
            "states": states,
            "viewport": [1280, 800],
        }

        sys.stdout.write(canonical_dumps(out_obj) + "\n")
        sys.stdout.flush()

        timing_obj = {
            "attempts": [1] * len(states),
            "attach_ms": round(attach_ms, 3),
            "capture_ms": [round(m, 3) for m in capture_ms_list],
            "lang": "python",
            "launch_ms": round(launch_ms, 3),
            "load_ms": round(load_ms, 3),
            "seed": "py_w2",
            "steps_ms": [round(m, 3) for m in steps_ms_list],
            "where": os.environ.get("X2_WHERE", "unlabelled"),
        }

        with open(args.timing, "w", encoding="utf-8") as f:
            f.write(canonical_dumps(timing_obj) + "\n")

    finally:
        try:
            client.send_command("Browser.close", timeout_s=2.0)
        except Exception:
            pass

        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()

        os.close(parent_write_fd)
        os.close(parent_read_fd)
        shutil.rmtree(user_data_dir, ignore_errors=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
