#!/usr/bin/env python3
from html.parser import HTMLParser
import json
import os
import re
import tempfile
import unittest

from gen_fixtures import generate_fixtures


class IdAndClassParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.classes = set()

    def handle_starttag(self, tag, attrs):
        for attr, val in attrs:
            if attr == "id" and val:
                self.ids.add(val)
            elif attr == "class" and val:
                for cls in val.split():
                    self.classes.add(cls)


class TestGenFixtures(unittest.TestCase):

    def test_byte_identical_runs(self):
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            generate_fixtures(tmp1)
            generate_fixtures(tmp2)

            files1 = sorted(os.listdir(tmp1))
            files2 = sorted(os.listdir(tmp2))
            self.assertEqual(files1, files2)

            for fname in files1:
                path1 = os.path.join(tmp1, fname)
                path2 = os.path.join(tmp2, fname)
                with open(path1, "rb") as f1, open(path2, "rb") as f2:
                    self.assertEqual(f1.read(), f2.read(), f"File {fname} is not byte-identical across runs")

    def test_steps_files_format_and_ops(self):
        allowed_ops = {"click", "type", "wait"}
        with tempfile.TemporaryDirectory() as tmpdir:
            generate_fixtures(tmpdir)
            steps_files = [f for f in os.listdir(tmpdir) if f.endswith(".steps.json")]
            self.assertEqual(len(steps_files), 5)

            for sf in steps_files:
                path = os.path.join(tmpdir, sf)
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                self.assertIn("steps", data, f"Key 'steps' missing in {sf}")
                self.assertIsInstance(data["steps"], list, f"'steps' in {sf} must be a list")

                for step in data["steps"]:
                    self.assertIn("op", step, f"Step missing 'op' in {sf}")
                    self.assertIn(step["op"], allowed_ops, f"Invalid op '{step['op']}' in {sf}")
                    self.assertIn("selector", step, f"Step missing 'selector' in {sf}")
                    if step["op"] == "type":
                        self.assertIn("text", step, f"Type step missing 'text' in {sf}")

    def test_selectors_exist_in_html(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generate_fixtures(tmpdir)
            page_names = ["p1_form", "p2_list", "p3_dialog", "p4_late", "p5_text"]

            for name in page_names:
                html_path = os.path.join(tmpdir, f"{name}.html")
                steps_path = os.path.join(tmpdir, f"{name}.steps.json")

                with open(html_path, "r", encoding="utf-8") as f:
                    html_content = f.read()

                parser = IdAndClassParser()
                parser.feed(html_content)

                with open(steps_path, "r", encoding="utf-8") as f:
                    steps_data = json.load(f)

                for step in steps_data["steps"]:
                    sel = step["selector"]
                    if sel.startswith("#"):
                        target_id = sel[1:]
                        self.assertIn(
                            target_id,
                            parser.ids,
                            f"Selector ID '{target_id}' from {name}.steps.json not found in {name}.html"
                        )
                    elif sel.startswith("."):
                        target_cls = sel[1:]
                        self.assertIn(
                            target_cls,
                            parser.classes,
                            f"Selector class '{target_cls}' from {name}.steps.json not found in {name}.html"
                        )

    def test_no_http_https_or_src(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generate_fixtures(tmpdir)
            html_files = [f for f in os.listdir(tmpdir) if f.endswith(".html")]
            self.assertEqual(len(html_files), 5)

            for hf in html_files:
                path = os.path.join(tmpdir, hf)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()

                self.assertNotIn("http:", content.lower(), f"Forbidden 'http:' found in {hf}")
                self.assertNotIn("https:", content.lower(), f"Forbidden 'https:' found in {hf}")

                # Check for src attribute (case-insensitive)
                src_match = re.search(r'\bsrc\s*=', content, re.IGNORECASE)
                self.assertIsNone(src_match, f"Forbidden 'src=' attribute found in {hf}")

    def test_never_writes_outside_out(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target_sub = os.path.join(tmpdir, "nested", "fixtures")
            generate_fixtures(target_sub)

            self.assertTrue(os.path.exists(target_sub))
            # Confirm no files were created in parent tmpdir outside nested/fixtures
            top_level = os.listdir(tmpdir)
            self.assertEqual(top_level, ["nested"])


if __name__ == "__main__":
    unittest.main()
