"""Integration: real Chromium over --remote-debugging-pipe on a file:// fixture.

Skipped (with a clear message) only when CHROME_BIN is unset. Offline: no network.
STORAGE LAW: no screenshot/frame is captured or written.
"""
import os
import shutil
import tempfile
import unittest

from wender.perceive import browser, cdp_pipe
from wender.state import schema

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Wender fixture</title></head>
<body><nav aria-label="Top"><a href="#a">Home</a></nav>
<main><h1>Fixture</h1>
<form aria-label="Login"><input type="text" aria-label="User">
<button type="button">Sign in</button><button type="button" disabled>Disabled</button></form>
<div style="display:none"><button>Invisible</button></div>
<article aria-label="Answer"><p>provider text</p><button>inner</button></article></main></body></html>"""


@unittest.skipUnless(os.environ.get("CHROME_BIN"),
                     "CHROME_BIN unset: set CHROME_BIN=/path/to/chrome to run the Chromium integration test")
class TestChromeState(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="waf", dir="/tmp")
        self.path = os.path.join(self.dir, "fixture.html")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(HTML)
        self.url = "file://" + self.path

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _state(self, c, sid):
        c.navigate(sid, self.url)
        return browser.page_state(c, sid)

    def test_state_from_real_page(self):
        with cdp_pipe.CdpPipe(timeout=30) as c:
            sid = c.attach_page()
            s1 = self._state(c, sid)
            s2 = self._state(c, sid)  # reload
            udd = c.user_data_dir
        self.assertFalse(os.path.exists(udd))
        self.assertEqual(schema.validate_state(s1), [])
        self.assertEqual(s1["source"]["kind"], "cdp")
        self.assertEqual(s1["source"]["title"], "Wender fixture")
        by = {(n["role"], n["name"]): n for n in s1["nodes"]}
        for key in [("navigation", "Top"), ("link", "Home"), ("main", ""), ("heading", "Fixture"),
                    ("form", "Login"), ("textbox", "User"), ("button", "Sign in"),
                    ("button", "Disabled"), ("article", "Answer")]:
            self.assertIn(key, by, key)
        self.assertNotIn(("button", "Invisible"), by)
        self.assertNotIn(("button", "inner"), by)          # opaque article
        self.assertTrue(by[("button", "Sign in")]["actionable"])
        self.assertFalse(by[("button", "Disabled")]["actionable"])
        b = by[("button", "Sign in")]["box"]
        self.assertIsNotNone(b)
        self.assertTrue(b[2] > 0 and b[3] > 0)
        self.assertIn(by[("button", "Sign in")]["id"], s1["actionable"])
        self.assertTrue(all(isinstance(v, int) for v in s1["viewport"].values()))
        # reload-stable identity
        self.assertEqual(s1["ax_sha256"], s2["ax_sha256"])
        self.assertEqual(s1["ax_stable_sha256"], s2["ax_stable_sha256"])
        self.assertEqual([n["id"] for n in s1["nodes"]], [n["id"] for n in s2["nodes"]])
        self.assertEqual(schema.dumps(s1), schema.dumps(s2))
        self.assertLess(len(schema.dumps(s1)), 8000)  # compact state, not a raw AX dump


if __name__ == "__main__":
    unittest.main()
