"""Pins for docs/spec/KNOWN_ISSUES_P1_WEB.md: each expectedFailure is a known gap; a fix removes the decorator."""
import importlib
import unittest

from wender.act import cdp_executor


def _has(module, name):
    try:
        return hasattr(importlib.import_module(module), name)
    except ImportError:
        return False


class KnownIssuesP1Web(unittest.TestCase):
    def test_wp1_02_ax_completion_waiter_exists(self):
        self.assertTrue(_has("wender.perceive.wait", "wait_done"))

    def test_wp1_03_executor_has_keyboard_ops(self):
        self.assertIn("press", cdp_executor.OPS)
        self.assertIn("insert_text", cdp_executor.OPS)

    def test_wp1_04_answer_text_extractor_exists(self):
        self.assertTrue(_has("wender.perceive.text", "extract"))

    def test_wp1_05_stop_state_detector_exists(self):
        self.assertTrue(_has("wender.perceive.stopstate", "detect"))

    def test_wp1_01_no_site_css_selectors_in_wender(self):
        # WP1-01 guard: AX only; site data attributes must never appear in wender code.
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1] / "wender"
        hits = [str(p) for p in root.rglob("*.py") if "data-message-author-role" in p.read_text(encoding="utf-8")]
        self.assertEqual(hits, [])


class CdpPortSafety(unittest.TestCase):
    def test_refuses_non_loopback(self):
        from wender.perceive.cdp_port import CdpPort
        from wender.perceive.cdp_pipe import CdpError
        with self.assertRaises(CdpError):
            CdpPort(port=9222, host="192.168.1.10")

    def test_close_never_sends_browser_close(self):
        from wender.perceive.cdp_port import CdpPort
        p = CdpPort()
        sent = []
        p.send = lambda method, params=None, session_id=None, timeout=None: sent.append(method) or {}
        p._attached = ["s1"]
        p.close()
        self.assertEqual(sent, ["Target.detachFromTarget"])


if __name__ == "__main__":
    unittest.main()
