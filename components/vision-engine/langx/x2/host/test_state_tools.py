import json
import subprocess
import sys
import tempfile
import unittest

from langx.x2.host.state_check import check_bytes
from langx.x2.host.state_diff import diff_states, actions_report


def make_valid_doc():
    return {
        "schema": "vqa.x2-state/v1",
        "page": "test.html",
        "viewport": [1280, 800],
        "states": [
            {
                "step": -1,
                "op": "load",
                "ok": True,
                "error": None,
                "nodes": [
                    {
                        "i": 0,
                        "tag": "input",
                        "id": "inp1",
                        "role": "textbox",
                        "name": "username",
                        "value": "",
                        "checked": False,
                        "box": [10, 20, 100, 50],
                        "act": True
                    }
                ]
            }
        ]
    }


def to_canonical_bytes(doc):
    return json.dumps(doc, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii') + b'\n'


class TestStateCheck(unittest.TestCase):
    def test_valid_document(self):
        doc = make_valid_doc()
        raw = to_canonical_bytes(doc)
        problems = check_bytes(raw)
        self.assertEqual(problems, [])

    def test_not_valid_utf8_or_pure_ascii(self):
        raw = b'{"schema":"vqa.x2-state/v1", "non_ascii":"\x80"}\n'
        problems = check_bytes(raw)
        self.assertTrue(any("not valid UTF-8 or not pure ASCII" in p for p in problems))

    def test_bytes_not_equal_canonical_reserialization(self):
        doc = make_valid_doc()
        # Not compact separators (space after colon)
        non_canonical = json.dumps(doc, indent=2).encode('ascii') + b'\n'
        problems = check_bytes(non_canonical)
        self.assertTrue(any("canonical" in p for p in problems))

    def test_missing_or_extra_keys(self):
        doc = make_valid_doc()
        doc["extra_top"] = 123
        raw = to_canonical_bytes(doc)
        problems = check_bytes(raw)
        self.assertTrue(any("missing or extra" in p for p in problems))

    def test_wrong_types(self):
        doc = make_valid_doc()
        doc["schema"] = 12345
        raw = to_canonical_bytes(doc)
        problems = check_bytes(raw)
        self.assertTrue(any("wrong types" in p for p in problems))

    def test_any_float(self):
        doc = make_valid_doc()
        doc["viewport"] = [1280.0, 800.0]
        # Custom canonical serialization with float
        raw = json.dumps(doc, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii') + b'\n'
        problems = check_bytes(raw)
        self.assertTrue(any("float" in p for p in problems))

    def test_box_invalid_bounds(self):
        doc = make_valid_doc()
        doc["states"][0]["nodes"][0]["box"] = [100, 200, 50, 300]  # x1 (50) < x0 (100)
        raw = to_canonical_bytes(doc)
        problems = check_bytes(raw)
        self.assertTrue(any("box with x1 less than x0" in p for p in problems))

    def test_index_not_equal_to_list_position(self):
        doc = make_valid_doc()
        doc["states"][0]["nodes"][0]["i"] = 5
        raw = to_canonical_bytes(doc)
        problems = check_bytes(raw)
        self.assertTrue(any("i not equal" in p for p in problems))

    def test_states_not_starting_with_step_minus1_op_load(self):
        doc = make_valid_doc()
        doc["states"][0]["step"] = 0
        doc["states"][0]["op"] = "click"
        raw = to_canonical_bytes(doc)
        problems = check_bytes(raw)
        self.assertTrue(any("states not starting with step -1 op load" in p for p in problems))


class TestStateDiff(unittest.TestCase):
    def test_insertion_in_middle_not_reported_as_change_of_later_nodes(self):
        n0 = {"i": 0, "tag": "div", "id": "head", "role": "banner", "name": "Header", "value": "", "checked": False, "box": [0, 0, 100, 50], "act": False}
        n1 = {"i": 1, "tag": "button", "id": "btn1", "role": "button", "name": "Submit", "value": "", "checked": False, "box": [0, 60, 50, 80], "act": True}
        n2 = {"i": 2, "tag": "footer", "id": "foot", "role": "contentinfo", "name": "Footer", "value": "", "checked": False, "box": [0, 100, 100, 120], "act": False}

        nodes1 = [n0, n1, n2]

        n_new = {"i": 1, "tag": "div", "id": "alert", "role": "alert", "name": "Notice", "value": "", "checked": False, "box": [0, 52, 100, 58], "act": False}
        n1_shifted = dict(n1, i=2)
        n2_shifted = dict(n2, i=3)

        nodes2 = [n0, n_new, n1_shifted, n2_shifted]

        delta = diff_states(nodes1, nodes2)
        self.assertEqual(delta["added"], [1])
        self.assertEqual(delta["removed"], [])
        self.assertEqual(delta["changed"], [])

    def test_moved_box(self):
        n1 = {"i": 0, "tag": "div", "id": "box1", "role": "region", "name": "Box", "value": "", "checked": False, "box": [10, 20, 50, 60], "act": False}
        n2 = {"i": 0, "tag": "div", "id": "box1", "role": "region", "name": "Box", "value": "", "checked": False, "box": [15, 30, 55, 70], "act": False}

        delta = diff_states([n1], [n2])
        self.assertEqual(delta["added"], [])
        self.assertEqual(delta["removed"], [])
        self.assertEqual(len(delta["changed"]), 1)
        ch = delta["changed"][0]
        self.assertEqual(ch["i1"], 0)
        self.assertEqual(ch["i2"], 0)
        self.assertEqual(ch["dx"], 5)
        self.assertEqual(ch["dy"], 10)
        self.assertEqual(ch["box"], [15, 30, 55, 70])

    def test_typed_value(self):
        n1 = {"i": 0, "tag": "input", "id": "user", "role": "textbox", "name": "Username", "value": "", "checked": False, "box": [10, 10, 100, 30], "act": True}
        n2 = {"i": 0, "tag": "input", "id": "user", "role": "textbox", "name": "Username", "value": "alice", "checked": False, "box": [10, 10, 100, 30], "act": True}

        delta = diff_states([n1], [n2])
        self.assertEqual(len(delta["changed"]), 1)
        self.assertEqual(delta["changed"][0]["value"], "alice")

    def test_checkbox_toggle(self):
        n1 = {"i": 0, "tag": "input", "id": "chk", "role": "checkbox", "name": "Agree", "value": "", "checked": False, "box": [10, 10, 20, 20], "act": True}
        n2 = {"i": 0, "tag": "input", "id": "chk", "role": "checkbox", "name": "Agree", "value": "", "checked": True, "box": [10, 10, 20, 20], "act": True}

        delta = diff_states([n1], [n2])
        self.assertEqual(len(delta["changed"]), 1)
        self.assertEqual(delta["changed"][0]["checked"], True)

    def test_removed_dialog(self):
        n0 = {"i": 0, "tag": "div", "id": "main", "role": "main", "name": "Main", "value": "", "checked": False, "box": [0, 0, 500, 500], "act": False}
        n_dlg = {"i": 1, "tag": "div", "id": "dlg", "role": "dialog", "name": "Confirm", "value": "", "checked": False, "box": [50, 50, 200, 200], "act": False}
        n_btn = {"i": 2, "tag": "button", "id": "ok_btn", "role": "button", "name": "OK", "value": "", "checked": False, "box": [100, 150, 150, 180], "act": True}

        nodes1 = [n0, n_dlg, n_btn]
        nodes2 = [n0]

        delta = diff_states(nodes1, nodes2)
        self.assertEqual(delta["added"], [])
        self.assertEqual(delta["removed"], [1, 2])
        self.assertEqual(delta["changed"], [])

    def test_identical_states_giving_empty_delta(self):
        n0 = {"i": 0, "tag": "div", "id": "main", "role": "main", "name": "Main", "value": "", "checked": False, "box": [0, 0, 500, 500], "act": False}
        nodes = [n0]

        delta = diff_states(nodes, nodes)
        self.assertEqual(delta, {"added": [], "changed": [], "removed": []})

    def test_determinism_across_two_calls(self):
        n1 = {"i": 0, "tag": "input", "id": "user", "role": "textbox", "name": "Username", "value": "", "checked": False, "box": [10, 10, 100, 30], "act": True}
        n2 = {"i": 0, "tag": "input", "id": "user", "role": "textbox", "name": "Username", "value": "bob", "checked": False, "box": [15, 10, 105, 30], "act": True}

        res1 = diff_states([n1], [n2])
        res2 = diff_states([n1], [n2])
        self.assertEqual(res1, res2)

        doc = make_valid_doc()
        doc["states"].append({
            "step": 0,
            "op": "type",
            "ok": True,
            "error": None,
            "nodes": [n2]
        })

        rep1 = actions_report(doc)
        rep2 = actions_report(doc)
        self.assertEqual(rep1, rep2)

    def test_cli_actions_report(self):
        doc = make_valid_doc()
        doc["states"].append({
            "step": 0,
            "op": "type",
            "ok": True,
            "error": None,
            "nodes": [
                {
                    "i": 0,
                    "tag": "input",
                    "id": "inp1",
                    "role": "textbox",
                    "name": "username",
                    "value": "alice",
                    "checked": False,
                    "box": [10, 20, 100, 50],
                    "act": True
                }
            ]
        })

        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            f.write(json.dumps(doc))
            tmp_name = f.name

        cmd = [sys.executable, "-m", "langx.x2.host.state_diff", tmp_name]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        output = proc.stdout.strip()
        parsed = json.loads(output)
        self.assertEqual(parsed["schema"], "vqa.x2-state/v1")
        self.assertEqual(len(parsed["steps"]), 1)
        self.assertEqual(parsed["steps"][0]["step"], 0)
        self.assertEqual(parsed["steps"][0]["op"], "type")
        self.assertEqual(parsed["steps"][0]["delta"]["changed"][0]["value"], "alice")


if __name__ == "__main__":
    unittest.main()
