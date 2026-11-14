import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from langx.x2.py_w2.x2 import (
    CDPClient,
    canonical_dumps,
    compute_backoff_delays,
    is_actionable,
    parse_box,
)

# If CHROME_BIN is unset, but the VM has a Chrome binary, set CHROME_BIN
if not os.environ.get("CHROME_BIN"):
    for candidate in ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]:
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            os.environ["CHROME_BIN"] = candidate
            break


class TestPureNULReader(unittest.TestCase):
    def test_message_split_byte_by_byte(self):
        r_fd, w_fd = os.pipe()
        proc = subprocess.Popen(["true"])
        try:
            client = CDPClient(proc=proc, p_write_fd=w_fd, p_read_fd=r_fd)
            client.pending_responses[1] = None

            msg = json.dumps({"id": 1, "result": {"ok": True}}).encode("utf-8") + b"\x00"
            for b in msg:
                os.write(w_fd, bytes([b]))
                client._read_from_pipe(timeout_s=0.01)

            self.assertIsNotNone(client.pending_responses.get(1))
            self.assertEqual(client.pending_responses[1]["result"]["ok"], True)
        finally:
            proc.wait()
            os.close(r_fd)
            os.close(w_fd)

    def test_three_messages_in_one_write(self):
        r_fd, w_fd = os.pipe()
        proc = subprocess.Popen(["true"])
        try:
            client = CDPClient(proc=proc, p_write_fd=w_fd, p_read_fd=r_fd)
            client.pending_responses[1] = None
            client.pending_responses[2] = None

            m1 = json.dumps({"id": 1, "result": {"seq": 1}}).encode("utf-8") + b"\x00"
            m2 = json.dumps({"method": "Page.loadEventFired"}).encode("utf-8") + b"\x00"
            m3 = json.dumps({"id": 2, "result": {"seq": 2}}).encode("utf-8") + b"\x00"

            os.write(w_fd, m1 + m2 + m3)
            client._read_from_pipe(timeout_s=0.05)

            self.assertEqual(client.pending_responses[1]["result"]["seq"], 1)
            self.assertEqual(client.pending_responses[2]["result"]["seq"], 2)
            self.assertEqual(len(client.event_queue), 1)
            self.assertEqual(client.event_queue[0]["method"], "Page.loadEventFired")
        finally:
            proc.wait()
            os.close(r_fd)
            os.close(w_fd)

    def test_torn_tail_exit_6(self):
        r_fd, w_fd = os.pipe()
        proc = subprocess.Popen(["true"])
        try:
            client = CDPClient(proc=proc, p_write_fd=w_fd, p_read_fd=r_fd)
            os.write(w_fd, b'{"id":1,"res')
            os.close(w_fd)

            client._read_from_pipe(timeout_s=0.05)
            with self.assertRaises(SystemExit) as cm:
                client._read_from_pipe(timeout_s=0.05)
            self.assertEqual(cm.exception.code, 6)
        finally:
            proc.wait()
            os.close(r_fd)

    def test_unknown_response_id_exit_7(self):
        r_fd, w_fd = os.pipe()
        proc = subprocess.Popen(["true"])
        try:
            client = CDPClient(proc=proc, p_write_fd=w_fd, p_read_fd=r_fd)
            m = json.dumps({"id": 99, "result": {}}).encode("utf-8") + b"\x00"
            os.write(w_fd, m)

            with self.assertRaises(SystemExit) as cm:
                client._read_from_pipe(timeout_s=0.05)
            self.assertEqual(cm.exception.code, 7)
        finally:
            proc.wait()
            os.close(r_fd)
            os.close(w_fd)


class TestPureCanonicalSerializer(unittest.TestCase):
    def test_canonical_serializer(self):
        test_cases = [
            {"b": 2, "a": 1, "text": "Héllo Wörld! 🚀 /path/to/file \n\t\r\b\f"},
            {"str": "música", "slash": "a/b/c", "control": "\u0001\u001f"},
            {"emoji": "😀", "chinese": "中文"},
        ]
        for obj in test_cases:
            expected = json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
            actual = canonical_dumps(obj)
            self.assertEqual(actual, expected)
            self.assertNotIn("/", actual.replace("/", ""))


class TestPureBoxRule(unittest.TestCase):
    def test_box_rule(self):
        self.assertEqual(parse_box(10.2, 20.7, 100.5, 50.3), [10, 20, 110, 70])
        self.assertIsNone(parse_box(0.0, 0.0, 0.0, 10.0))
        self.assertIsNone(parse_box(0.0, 0.0, 10.0, 0.0))
        self.assertIsNone(parse_box(10.0, 10.0, -5.0, 20.0))
        self.assertEqual(parse_box(-10.5, -5.2, 30.0, 40.0), [-11, -6, 19, 34])


class TestPureBackoffSequence(unittest.TestCase):
    def test_backoff_delays(self):
        delays = compute_backoff_delays()
        self.assertEqual(delays, [1, 2, 4, 8, 16, 32, 64])

        timeout_ms = 200.0
        elapsed = 0.0
        idx = 0
        seq = []
        while elapsed < timeout_ms:
            d = delays[min(idx, len(delays) - 1)]
            idx += 1
            rem = timeout_ms - elapsed
            if rem <= 0:
                break
            actual = min(d, rem)
            seq.append(actual)
            elapsed += actual

        self.assertEqual(seq[:7], [1, 2, 4, 8, 16, 32, 64])
        self.assertEqual(seq[7], 64)
        self.assertAlmostEqual(sum(seq), 200.0)


class TestPureActionableRule(unittest.TestCase):
    def test_actionable(self):
        table = [
            ([0, 0, 10, 10], {}, "button", "", True),
            ([0, 0, 10, 10], {"disabled": ""}, "button", "", False),
            (None, {}, "button", "", False),
            ([0, 0, 10, 10], {}, "a", "", False),
            ([0, 0, 10, 10], {"href": "#"}, "a", "", True),
            ([0, 0, 10, 10], {"type": "hidden"}, "input", "", False),
            ([0, 0, 10, 10], {"type": "text"}, "input", "", True),
            ([0, 0, 10, 10], {}, "div", "button", True),
            ([0, 0, 10, 10], {}, "div", "link", True),
            ([0, 0, 10, 10], {}, "div", "", False),
            ([0, 0, 10, 10], {}, "select", "", True),
            ([0, 0, 10, 10], {}, "textarea", "", True),
            ([0, 0, 10, 10], {}, "summary", "", True),
        ]
        for box, attrs, tag, role, expected in table:
            with self.subTest(tag=tag, role=role, attrs=attrs, box=box):
                self.assertEqual(is_actionable(box, attrs, tag, role), expected)


class TestBrowserIntegration(unittest.TestCase):
    def test_browser_steps(self):
        chrome_bin = os.environ.get("CHROME_BIN")
        if not chrome_bin:
            self.skipTest("SKIP CHROME_BIN unset: browser binary not set or found")

        env = dict(os.environ)
        env["CHROME_BIN"] = chrome_bin

        temp_dir = tempfile.mkdtemp(prefix="x2_test_")
        try:
            html_path = os.path.join(temp_dir, "test.html")
            html_content = """<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
  <input id="my-input" type="text" value="" />
  <button id="my-button" onclick="document.getElementById('my-div').textContent = 'Clicked!'">Click Me</button>
  <div id="my-div" role="button">Initial</div>
  <script>
    setTimeout(() => {
      const el = document.createElement('button');
      el.id = 'late-element';
      el.textContent = 'Late Content';
      document.body.appendChild(el);
    }, 1500);
  </script>
</body>
</html>
"""
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            steps_path = os.path.join(temp_dir, "steps.json")
            steps_content = {
                "steps": [
                    {"op": "type", "selector": "#my-input", "text": "Hello Browser", "timeout_ms": 2000},
                    {"op": "click", "selector": "#my-button", "timeout_ms": 2000},
                    {"op": "wait", "selector": "#late-element", "timeout_ms": 3000},
                ]
            }
            with open(steps_path, "w", encoding="utf-8") as f:
                json.dump(steps_content, f)

            timing1_path = os.path.join(temp_dir, "timing1.json")
            timing2_path = os.path.join(temp_dir, "timing2.json")

            x2_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "x2.py"))

            # First run
            res1 = subprocess.run(
                [
                    sys.executable,
                    x2_script,
                    "--page",
                    html_path,
                    "--steps",
                    steps_path,
                    "--timing",
                    timing1_path,
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(res1.returncode, 0, f"Run 1 stderr: {res1.stderr}")

            # Second run
            res2 = subprocess.run(
                [
                    sys.executable,
                    x2_script,
                    "--page",
                    html_path,
                    "--steps",
                    steps_path,
                    "--timing",
                    timing2_path,
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(res2.returncode, 0, f"Run 2 stderr: {res2.stderr}")

            # Verify byte-equal stdout across runs
            self.assertEqual(res1.stdout, res2.stdout)

            # Parse state output
            out = json.loads(res1.stdout)
            self.assertEqual(out.get("schema"), "vqa.x2-state/v1")
            self.assertEqual(out.get("viewport"), [1280, 800])
            self.assertEqual(out.get("page"), "test.html")

            states = out.get("states", [])
            self.assertEqual(len(states), 4)

            # Assert state -1 (load)
            s_load = states[0]
            self.assertEqual(s_load["step"], -1)
            self.assertEqual(s_load["op"], "load")
            self.assertTrue(s_load["ok"])

            # Assert state 0 (type)
            s_type = states[1]
            self.assertEqual(s_type["step"], 0)
            self.assertEqual(s_type["op"], "type")
            self.assertTrue(s_type["ok"])

            input_node = next((n for n in s_type["nodes"] if n["id"] == "my-input"), None)
            self.assertIsNotNone(input_node)
            self.assertEqual(input_node["value"], "Hello Browser")

            # Assert state 1 (click)
            s_click = states[2]
            self.assertEqual(s_click["step"], 1)
            self.assertEqual(s_click["op"], "click")
            self.assertTrue(s_click["ok"])

            div_node = next((n for n in s_click["nodes"] if n["id"] == "my-div"), None)
            self.assertIsNotNone(div_node)
            self.assertEqual(div_node["name"], "Clicked!")

            # Assert state 2 (wait)
            s_wait = states[3]
            self.assertEqual(s_wait["step"], 2)
            self.assertEqual(s_wait["op"], "wait")
            self.assertTrue(s_wait["ok"])

            late_node_in_click = next((n for n in s_click["nodes"] if n["id"] == "late-element"), None)
            self.assertIsNone(late_node_in_click)

            late_node_in_wait = next((n for n in s_wait["nodes"] if n["id"] == "late-element"), None)
            self.assertIsNotNone(late_node_in_wait)
            self.assertEqual(late_node_in_wait["name"], "Late Content")

            # Verify timing files
            self.assertTrue(os.path.exists(timing1_path))
            with open(timing1_path, "r", encoding="utf-8") as f:
                tdata = json.load(f)
            self.assertEqual(tdata.get("lang"), "python")
            self.assertEqual(tdata.get("seed"), "py_w2")
            self.assertIn("launch_ms", tdata)
            self.assertIn("attach_ms", tdata)
            self.assertIn("load_ms", tdata)
            self.assertIn("capture_ms", tdata)
            self.assertIn("steps_ms", tdata)

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
