import os
import sys
import json
import time
import shutil
import tempfile
import unittest
import subprocess

from langx.x2.py_w1.x2 import (
    canonical_dumps,
    compute_box,
    build_nodes_list,
    get_backoff_ms,
    PipeFramingReader,
    CDPClient
)


class TestPureX2(unittest.TestCase):
    def test_canonical_serializer(self):
        sample = {
            "title": "Café & Crème / 😃",
            "escapes": "Line 1\nLine 2\tTab\rCR\bBS\fFF",
            "url": "http://example.com/path",
            "number": 123
        }
        expected = json.dumps(sample, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
        actual = canonical_dumps(sample)
        self.assertEqual(actual, expected)

        # Rule: slash is never escaped in output JSON string
        # Non-ASCII escaped as \uXXXX (including surrogate pairs for emoji)
        self.assertIn("http://example.com/path", actual)
        self.assertIn("Caf\\u00e9 & Cr\\u00e8me / \\ud83d\\ude03", actual)

    def test_compute_box_rule(self):
        # Closed integer box [x0, y0, x1, y1]
        # x0 = floor(x), y0 = floor(y)
        # x1 = max(x0, ceil(x + w - 1)), y1 = max(y0, ceil(y + h - 1))
        self.assertIsNone(compute_box(10.0, 10.0, 0, 10))
        self.assertIsNone(compute_box(10.0, 10.0, 10, -5))

        box = compute_box(10.2, 20.7, 100.5, 50.3)
        self.assertEqual(box, [10, 20, 110, 70])

    def test_backoff_sequence_and_timeout(self):
        # Sequence: 1, 2, 4, 8, 16, 32, 64, 64, 64...
        seq = [get_backoff_ms(i) for i in range(10)]
        self.assertEqual(seq, [1, 2, 4, 8, 16, 32, 64, 64, 64, 64])

        # Accumulate total under a 100ms timeout
        timeout_ms = 100
        total_ms = 0
        step = 0
        while total_ms < timeout_ms:
            ms = get_backoff_ms(step)
            if total_ms + ms > timeout_ms:
                break
            total_ms += ms
            step += 1

        self.assertEqual(total_ms, 63) # 1+2+4+8+16+32 = 63
        self.assertEqual(step, 6)

    def test_actionable_rule_table(self):
        # Verify actionable rule mapping
        strings = ["div", "button", "a", "input", "p", "id", "b1", "a1", "i1", "p1", "href", "http://x", "disabled", "true", "type", "text"]
        snap_doc = {
            "nodes": {
                "nodeName": [0, 1, 2, 3, 4], # div, button, a, input, p
                "backendNodeId": [10, 11, 12, 13, 14],
                "attributes": [
                    [],
                    [5, 6], # id=b1
                    [5, 7, 10, 11], # id=a1, href=http://x
                    [5, 8, 14, 15], # id=i1, type=text
                    [5, 9] # id=p1
                ]
            },
            "layout": {
                "nodeIndex": [0, 1, 2, 3, 4],
                "bounds": [
                    [0, 0, 100, 100],
                    [0, 0, 50, 20],
                    [0, 0, 50, 20],
                    [0, 0, 50, 20],
                    [0, 0, 50, 20]
                ]
            }
        }
        ax_nodes = [
            {"backendDOMNodeId": 14, "role": {"value": "button"}, "name": {"value": "Click P"}}
        ]

        recs = build_nodes_list(snap_doc, strings, ax_nodes)
        rec_map = {r["id"]: r for r in recs if r["id"]}

        div_rec = [r for r in recs if r["tag"] == "div"][0]
        self.assertFalse(div_rec["act"])

        self.assertTrue(rec_map["b1"]["act"])
        self.assertTrue(rec_map["a1"]["act"])
        self.assertTrue(rec_map["i1"]["act"])
        self.assertTrue(rec_map["p1"]["act"])

    def test_nul_framing_reader_three_messages_in_one_write(self):
        r, w = os.pipe()
        reader = PipeFramingReader(r)

        msg1 = json.dumps({"id": 1, "result": "m1"}).encode('utf-8') + b'\x00'
        msg2 = json.dumps({"id": 2, "result": "m2"}).encode('utf-8') + b'\x00'
        msg3 = json.dumps({"id": 3, "result": "m3"}).encode('utf-8') + b'\x00'

        # Write three messages in one write call
        os.write(w, msg1 + msg2 + msg3)

        res1 = reader.read_message(timeout=1.0)
        res2 = reader.read_message(timeout=1.0)
        res3 = reader.read_message(timeout=1.0)

        self.assertEqual(res1, {"id": 1, "result": "m1"})
        self.assertEqual(res2, {"id": 2, "result": "m2"})
        self.assertEqual(res3, {"id": 3, "result": "m3"})

        os.close(r)
        os.close(w)

    def test_nul_framing_reader_byte_by_byte(self):
        r, w = os.pipe()
        reader = PipeFramingReader(r)

        full_msg = json.dumps({"id": 42, "status": "byte_by_byte"}).encode('utf-8') + b'\x00'

        # Write message one byte at a time
        for b in full_msg:
            os.write(w, bytes([b]))

        res = reader.read_message(timeout=1.0)
        self.assertEqual(res, {"id": 42, "status": "byte_by_byte"})

        os.close(r)
        os.close(w)

    def test_nul_framing_torn_tail_exit6(self):
        r, w = os.pipe()
        reader = PipeFramingReader(r)

        os.write(w, b'{"id": 1, "result": "partial"')
        os.close(w)

        with self.assertRaises(SystemExit) as cm:
            reader.read_message(timeout=1.0)
        self.assertEqual(cm.exception.code, 6)

        os.close(r)

    def test_unknown_response_id_exit7(self):
        r_parent, w_child = os.pipe()
        r_child, w_parent = os.pipe()

        client = CDPClient(w_parent, r_parent)

        bad_response = json.dumps({"id": 999, "result": {}}).encode('utf-8') + b'\x00'
        os.write(w_child, bad_response)

        with self.assertRaises(SystemExit) as cm:
            client.call("Target.getTargets", timeout=1.0)
        self.assertEqual(cm.exception.code, 7)

        os.close(r_parent)
        os.close(w_child)
        os.close(r_child)
        os.close(w_parent)


class TestBrowserX2(unittest.TestCase):
    def setUp(self):
        self.chrome_bin = os.environ.get("CHROME_BIN")
        if not self.chrome_bin:
            self.skipTest("SKIP CHROME_BIN unset")

    def test_browser_interaction_flow(self):
        tmp_dir = tempfile.mkdtemp(prefix="x2_test_")
        try:
            html_path = os.path.join(tmp_dir, "page.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write("""<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
  <input id="myinput" type="text" value="">
  <button id="mybtn" onclick="document.getElementById('mydiv').innerText = 'Clicked!';">Click Me</button>
  <div id="mydiv">Initial</div>
  <script>
    setTimeout(() => {
      const el = document.createElement('div');
      el.id = 'late_elem';
      el.innerText = 'Late Arrived';
      document.body.appendChild(el);
    }, 1500);
  </script>
</body>
</html>""")

            steps_path = os.path.join(tmp_dir, "steps.json")
            with open(steps_path, "w", encoding="utf-8") as f:
                json.dump({
                    "steps": [
                        {"op": "type", "selector": "#myinput", "text": "Hello World", "timeout_ms": 2000},
                        {"op": "click", "selector": "#mybtn", "timeout_ms": 2000},
                        {"op": "wait", "selector": "#late_elem", "timeout_ms": 3000}
                    ]
                }, f)

            timing_path1 = os.path.join(tmp_dir, "timing1.json")
            timing_path2 = os.path.join(tmp_dir, "timing2.json")

            cmd = [
                sys.executable,
                "langx/x2/py_w1/x2.py",
                "--page", html_path,
                "--steps", steps_path,
                "--timing", timing_path1,
                "--timeout-s", "30"
            ]

            env = dict(os.environ)
            res1 = subprocess.run(cmd, capture_output=True, text=True, env=env)
            self.assertEqual(res1.returncode, 0, f"stderr: {res1.stderr}")

            out1 = res1.stdout.strip()
            data1 = json.loads(out1)

            self.assertEqual(data1.get("schema"), "vqa.x2-state/v1")
            self.assertEqual(data1.get("page"), "page.html")
            self.assertEqual(data1.get("viewport"), [1280, 800])

            states = data1.get("states", [])
            self.assertEqual(len(states), 4) # initial + 3 steps

            self.assertEqual(states[0]["step"], -1)
            self.assertEqual(states[0]["op"], "load")
            self.assertTrue(states[0]["ok"])

            self.assertEqual(states[1]["step"], 0)
            self.assertEqual(states[1]["op"], "type")
            self.assertTrue(states[1]["ok"])
            inp_node = [n for n in states[1]["nodes"] if n["id"] == "myinput"][0]
            self.assertEqual(inp_node["value"], "Hello World")

            self.assertEqual(states[2]["step"], 1)
            self.assertEqual(states[2]["op"], "click")
            self.assertTrue(states[2]["ok"])

            self.assertEqual(states[3]["step"], 2)
            self.assertEqual(states[3]["op"], "wait")
            self.assertTrue(states[3]["ok"])
            late_node = [n for n in states[3]["nodes"] if n["id"] == "late_elem"]
            self.assertEqual(len(late_node), 1)

            cmd2 = [
                sys.executable,
                "langx/x2/py_w1/x2.py",
                "--page", html_path,
                "--steps", steps_path,
                "--timing", timing_path2,
                "--timeout-s", "30"
            ]
            res2 = subprocess.run(cmd2, capture_output=True, text=True, env=env)
            self.assertEqual(res2.returncode, 0, f"stderr: {res2.stderr}")
            out2 = res2.stdout.strip()

            self.assertEqual(out1, out2)

        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
