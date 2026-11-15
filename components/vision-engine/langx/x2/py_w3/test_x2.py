import os
import sys
import json
import time
import shutil
import tempfile
import unittest
import subprocess

from langx.x2.py_w3.x2 import (
    canonical_dumps,
    parse_box,
    is_actionable,
    get_backoff_sequence,
    PipeCDPClient
)


class TestPureFunctions(unittest.TestCase):
    def test_canonical_serializer(self):
        sample = {
            "slash": "a/b/c",
            "controls": "hello\nworld\ttab\bback\fform\rreturn\x01ctrl",
            "unicode": "café",
            "emoji": "😀",
            "nested": {"b": 2, "a": 1}
        }
        res = canonical_dumps(sample)
        expected = json.dumps(sample, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
        self.assertEqual(res, expected)
        self.assertIn("/b/", res)
        self.assertNotIn("\\/", res)

    def test_box_rule(self):
        box1 = parse_box([10.2, 20.7, 30.1, 40.5])
        self.assertEqual(box1, [10, 20, 40, 61])

        self.assertIsNone(parse_box([10, 20, 0, 50]))
        self.assertIsNone(parse_box([10, 20, 50, 0]))

        self.assertIsNone(parse_box([10, 20, -5, 50]))
        box_neg = parse_box([-10.5, -20.2, 15.3, 25.1])
        self.assertEqual(box_neg, [-11, -21, 4, 4])

    def test_backoff_sequence(self):
        delays, total = get_backoff_sequence(200)
        self.assertEqual(delays, [1, 2, 4, 8, 16, 32, 64, 64, 9])
        self.assertEqual(total, 200)

    def test_actionable_rule(self):
        cases = [
            ("button", "", [0, 0, 10, 10], False, {}, True),
            ("button", "", [0, 0, 10, 10], True, {}, False),
            ("div", "", [0, 0, 10, 10], False, {}, False),
            ("a", "", [0, 0, 10, 10], False, {"href": "#"}, True),
            ("a", "", [0, 0, 10, 10], False, {}, False),
            ("input", "", [0, 0, 10, 10], False, {"type": "text"}, True),
            ("input", "", [0, 0, 10, 10], False, {"type": "hidden"}, False),
            ("div", "button", [0, 0, 10, 10], False, {}, True),
            ("div", "link", [0, 0, 10, 10], False, {}, True),
            ("div", "unknown", [0, 0, 10, 10], False, {}, False),
            ("button", "", None, False, {}, False),
        ]
        for tag, role, box, is_disabled, attrs, expected in cases:
            with self.subTest(tag=tag, role=role, disabled=is_disabled):
                act = is_actionable(tag, role, box, is_disabled, attrs)
                self.assertEqual(act, expected)


class TestFramingAndProtocol(unittest.TestCase):
    def test_nul_framing_reader_split_and_multiple(self):
        r_fd, w_fd = os.pipe()

        client = PipeCDPClient("fake_chrome", tempfile.gettempdir())
        client.p2_r = r_fd
        client.p1_w = w_fd

        msg1 = {"id": 1, "result": {}}
        msg2 = {"id": 2, "result": {}}
        msg3 = {"id": 3, "result": {}}

        client.outstanding_ids.update({1, 2, 3})

        bytes1 = json.dumps(msg1).encode("utf-8") + b"\x00"
        bytes2 = json.dumps(msg2).encode("utf-8") + b"\x00"
        bytes3 = json.dumps(msg3).encode("utf-8") + b"\x00"

        for i in range(len(bytes1)):
            os.write(w_fd, bytes1[i:i+1])
            client.poll_reads(0.001)

        self.assertIn(1, client.pending_responses)

        os.write(w_fd, bytes2 + bytes3)
        client.poll_reads(0.001)

        self.assertIn(2, client.pending_responses)
        self.assertIn(3, client.pending_responses)

        os.close(w_fd)
        os.close(r_fd)

    def test_torn_tail_at_eof_gives_code_6(self):
        r_fd, w_fd = os.pipe()

        os.write(w_fd, b'{"id":1, "res')
        os.close(w_fd)

        cmd = [
            sys.executable, "-c",
            "import os, sys, json; "
            "from langx.x2.py_w3.x2 import PipeCDPClient; "
            "c = PipeCDPClient('fake', '/tmp'); c.p2_r = " + str(r_fd) + "; "
            "c.poll_reads(0.1); c.poll_reads(0.1)"
        ]

        proc = subprocess.Popen(cmd, pass_fds=(r_fd,))
        os.close(r_fd)
        proc.wait()
        self.assertEqual(proc.returncode, 6)

    def test_unknown_response_id_gives_code_7(self):
        r_fd, w_fd = os.pipe()

        msg = json.dumps({"id": 99, "result": {}}).encode("utf-8") + b"\x00"
        os.write(w_fd, msg)
        os.close(w_fd)

        cmd = [
            sys.executable, "-c",
            "import os, sys, json; "
            "from langx.x2.py_w3.x2 import PipeCDPClient; "
            "c = PipeCDPClient('fake', '/tmp'); c.p2_r = " + str(r_fd) + "; "
            "c.poll_reads(0.1)"
        ]

        proc = subprocess.Popen(cmd, pass_fds=(r_fd,))
        os.close(r_fd)
        proc.wait()
        self.assertEqual(proc.returncode, 7)


class TestBrowserIntegration(unittest.TestCase):
    def test_browser_steps_and_determinism(self):
        chrome_bin = os.environ.get("CHROME_BIN")
        if not chrome_bin:
            self.skipTest("SKIP CHROME_BIN unset")

        tmp_dir = tempfile.mkdtemp(prefix="x2_test_browser_")
        try:
            html_path = os.path.join(tmp_dir, "index.html")
            steps_path = os.path.join(tmp_dir, "steps.json")
            timing_path1 = os.path.join(tmp_dir, "timing1.json")
            timing_path2 = os.path.join(tmp_dir, "timing2.json")

            with open(html_path, "w", encoding="utf-8") as f:
                f.write("""<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
  <input id="in" type="text" value="initial">
  <button id="btn" onclick="var d = document.getElementById('div1'); d.setAttribute('aria-label', 'clicked text'); d.innerText = 'clicked text';">Click Me</button>
  <div id="div1" aria-label="original text">original text</div>
  <script>
    setTimeout(function() {
      var d = document.createElement('div');
      d.id = 'late';
      d.innerText = 'late element';
      document.body.appendChild(d);
    }, 1500);
  </script>
</body>
</html>""")

            steps_data = {
                "steps": [
                    {"op": "type", "selector": "#in", "text": " new_val", "timeout_ms": 2000},
                    {"op": "click", "selector": "#btn", "timeout_ms": 2000},
                    {"op": "wait", "selector": "#late", "timeout_ms": 3000}
                ]
            }

            with open(steps_path, "w", encoding="utf-8") as f:
                json.dump(steps_data, f)

            # Run 1
            cmd1 = [
                sys.executable, "langx/x2/py_w3/x2.py",
                "--page", html_path,
                "--steps", steps_path,
                "--timing", timing_path1
            ]

            res1 = subprocess.run(cmd1, capture_output=True, text=True)
            self.assertEqual(res1.returncode, 0, f"Run 1 failed stderr: {res1.stderr}")

            out1_str = res1.stdout.strip()
            data1 = json.loads(out1_str)

            self.assertEqual(data1.get("schema"), "vqa.x2-state/v1")
            self.assertEqual(data1.get("viewport"), [1280, 800])
            self.assertEqual(data1.get("page"), "index.html")

            states1 = data1.get("states", [])
            self.assertEqual(len(states1), 4)

            # State 0: initial
            self.assertEqual(states1[0]["op"], "load")
            self.assertEqual(states1[0]["step"], -1)
            self.assertTrue(states1[0]["ok"])

            # State 1: type
            self.assertEqual(states1[1]["op"], "type")
            self.assertEqual(states1[1]["step"], 0)
            self.assertTrue(states1[1]["ok"])
            in_node_1 = next((n for n in states1[1]["nodes"] if n["id"] == "in"), None)
            self.assertIsNotNone(in_node_1)
            self.assertIn("new_val", in_node_1["value"])

            # State 2: click
            self.assertEqual(states1[2]["op"], "click")
            self.assertEqual(states1[2]["step"], 1)
            self.assertTrue(states1[2]["ok"])
            div_node_2 = next((n for n in states1[2]["nodes"] if n["id"] == "div1"), None)
            self.assertIsNotNone(div_node_2)
            self.assertIn("clicked text", div_node_2["name"])

            # State 3: wait
            self.assertEqual(states1[3]["op"], "wait")
            self.assertEqual(states1[3]["step"], 2)
            self.assertTrue(states1[3]["ok"])
            late_node_0 = next((n for n in states1[0]["nodes"] if n["id"] == "late"), None)
            self.assertIsNone(late_node_0)
            late_node_3 = next((n for n in states1[3]["nodes"] if n["id"] == "late"), None)
            self.assertIsNotNone(late_node_3)

            # Run 2 for byte-equal stdout determinism
            cmd2 = [
                sys.executable, "langx/x2/py_w3/x2.py",
                "--page", html_path,
                "--steps", steps_path,
                "--timing", timing_path2
            ]

            res2 = subprocess.run(cmd2, capture_output=True, text=True)
            self.assertEqual(res2.returncode, 0, f"Run 2 failed stderr: {res2.stderr}")

            out2_str = res2.stdout.strip()
            self.assertEqual(out1_str, out2_str, "Stdout not byte-equal across runs")

            self.assertTrue(os.path.exists(timing_path1))
            with open(timing_path1, "r", encoding="utf-8") as f:
                timing_data = json.load(f)
            self.assertEqual(timing_data.get("seed"), "py_w3")
            self.assertEqual(timing_data.get("lang"), "python")
            self.assertIn("launch_ms", timing_data)
            self.assertIn("attach_ms", timing_data)
            self.assertIn("load_ms", timing_data)
            self.assertIn("capture_ms", timing_data)
            self.assertIn("steps_ms", timing_data)

        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
