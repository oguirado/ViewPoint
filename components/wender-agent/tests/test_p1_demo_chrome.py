"""P1 Demo Chrome integration test and candidate_actions unit tests.

Skipped (with a clear message) when CHROME_BIN is unset or when explore is unavailable and WA2_ALLOW_SKIP=1.
STORAGE LAW: frames and screenshots live only in RAM, never on disk.
"""
import os
import re
import shutil
import tempfile
import unittest

from wender.learn.chrome_driver import ChromeDriver
from wender.perceive import cdp_pipe, vision_adapter
from wender.state import schema

try:
    from wender.learn import explore
    HAS_EXPLORE = True
except ImportError:
    HAS_EXPLORE = False

HOME_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Home</title></head>
<body><h1>Home Page</h1>
<nav><a href="list.html">List</a> | <a href="settings.html">Settings</a></nav>
</body></html>"""

LIST_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>List</title></head>
<body><h1>Item List</h1>
<nav><a href="home.html">Home</a></nav>
<main><a href="item.html">Item Details</a></main>
</body></html>"""

ITEM_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Item</title></head>
<body><h1>Item Details</h1>
<nav><a href="list.html">Back to List</a></nav>
<main><button type="button">Delete Item</button></main>
</body></html>"""

SETTINGS_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Settings</title></head>
<body><h1>Settings Page</h1>
<nav><a href="home.html">Home</a></nav>
</body></html>"""


class TestChromeDriverUnit(unittest.TestCase):
    """Offline unit tests for ChromeDriver candidate_actions and irreversible marking."""

    def test_candidate_actions_and_irreversible_marking(self):
        pruned_nodes = [
            {"id": "0000000000000001", "role": "heading", "name": "Title", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000002", "role": "button", "name": "Submit", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000003", "role": "link", "name": "Delete account", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000004", "role": "button", "name": "Remove item", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000005", "role": "button", "name": "Pay now", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000006", "role": "link", "name": "Settings", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000007", "role": "button", "name": "Disabled button", "depth": 0, "ordinal": 0, "attrs": (("disabled", "true"),)},
            {"id": "0000000000000008", "role": "textbox", "name": "Username", "depth": 0, "ordinal": 0, "attrs": ()},
        ]
        boxes = {
            "0000000000000001": [0, 0, 100, 20],
            "0000000000000002": [0, 20, 50, 20],
            "0000000000000003": [0, 40, 50, 20],
            "0000000000000004": [0, 60, 50, 20],
            "0000000000000005": [0, 80, 50, 20],
            "0000000000000006": [0, 100, 50, 20],
            "0000000000000007": [0, 120, 50, 20],
            "0000000000000008": [0, 140, 100, 20],
        }
        state = schema.build_state(
            pruned_nodes,
            kind="cdp",
            url="file:///app/home.html",
            title="App",
            ax_sha256="a" * 64,
            ax_stable_sha256="b" * 64,
            prune={"name": "prune_ax_v1", "version": 1},
            raw_count=len(pruned_nodes),
            boxes=boxes,
            viewport={"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0, "doc_w": 800, "doc_h": 600, "dpr_ppm": 1000000},
            actionable_roles={"button", "link", "textbox"},
        )

        driver = ChromeDriver(None, None, None)
        actions = driver.candidate_actions(state)

        self.assertEqual(
            actions,
            [
                {"op": "click", "target": "0000000000000002", "irreversible": False},
                {"op": "click", "target": "0000000000000003", "irreversible": True},
                {"op": "click", "target": "0000000000000004", "irreversible": True},
                {"op": "click", "target": "0000000000000005", "irreversible": True},
                {"op": "click", "target": "0000000000000006", "irreversible": False},
                {"op": "click", "target": "0000000000000008", "irreversible": False},
            ],
        )


@unittest.skipUnless(
    os.environ.get("CHROME_BIN"),
    "CHROME_BIN unset: set CHROME_BIN=/path/to/chrome to run the Chromium integration test",
)
class TestP1DemoChrome(unittest.TestCase):
    """End-to-end integration test for ChromeDriver and explore on a 4-page HTML app."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wa_demo_", dir="/tmp")
        self.files = {
            "home.html": HOME_HTML,
            "list.html": LIST_HTML,
            "item.html": ITEM_HTML,
            "settings.html": SETTINGS_HTML,
        }
        for name, html in self.files.items():
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(html)
        self.home_url = "file://" + os.path.join(self.dir, "home.html")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_chrome_driver_with_fake_vision(self):
        fake_vision = vision_adapter.FakeVisionEngine()
        with cdp_pipe.CdpPipe(timeout=30) as cdp:
            sid = cdp.attach_page()
            driver = ChromeDriver(cdp, sid, self.home_url, vision=fake_vision)
            driver.reset()
            state, screen_fp = driver.observe()

            self.assertEqual(schema.validate_state(state), [])
            self.assertIsNotNone(screen_fp)
            self.assertRegex(screen_fp, re.compile(r"^[0-9a-f]{16}$"))

    def test_p1_demo_explore_loop(self):
        if not HAS_EXPLORE:
            if os.environ.get("WA2_ALLOW_SKIP") == "1":
                raise unittest.SkipTest("wa2-jules-explore-loop not available")
            else:
                self.fail("explore.py module wa.learn.explore not found")

        with cdp_pipe.CdpPipe(timeout=30) as cdp:
            sid = cdp.attach_page()
            driver = ChromeDriver(cdp, sid, self.home_url)
            driver.reset()

            res1 = explore.explore(driver, "p1_demo_app", max_steps=24, reset=driver.reset)
            pmap1 = res1["program_map"]

            # Assert 4 distinct program map nodes (home, list, item, settings)
            self.assertEqual(len(pmap1.nodes), 4)

            # Assert each navigation edge step has a non-empty changed summary
            self.assertTrue(len(res1["steps"]) > 0)
            for step in res1["steps"]:
                self.assertTrue(isinstance(step["changed"], str) and len(step["changed"]) > 0)

            # Assert Delete button was skipped
            if "skipped" in res1:
                self.assertTrue(len(res1["skipped"]) > 0)

            # Assert a second run gives identical program map JSON
            driver.reset()
            res2 = explore.explore(driver, "p1_demo_app", max_steps=24, reset=driver.reset)
            pmap2 = res2["program_map"]

            self.assertEqual(pmap1.to_json(), pmap2.to_json())


if __name__ == "__main__":
    unittest.main()
