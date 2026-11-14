# @module {"id":"wa.tests.test_fx2_f1_j5_browser_atspi_cleanup_contracts","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.browser","wa.perceive.atspi","wa.perceive.ax"],"book":"HB-WA-s1-perceive"}
"""Adversarial claim-pinning unit tests for browser page_state cleanup and AT-SPI family-PID fallback contracts.

Covers fix: fx2-f1-ax-browser-atspi-hardening.
"""
import json
import unittest
from unittest.mock import patch

from wender.perceive import atspi, browser, ax
from wender.state import schema


class FakeCdp:
    """Offline fake CDP transport recording sent methods and simulating targeted failures or disable errors."""

    def __init__(self, fail_at=None, release_error=None):
        self.sent_methods = []
        self.release_count = 0
        self.release_count_during_build = None
        self.fail_at = fail_at
        self.release_error = release_error

    def send(self, method, params=None, session_id=None):
        self.sent_methods.append(method)
        if method == "Accessibility.enable":
            return {}
        elif method == "Accessibility.getFullAXTree":
            if self.fail_at == "raw_tree":
                raise RuntimeError("raw_tree failed")
            return {
                "nodes": [
                    {
                        "nodeId": "1",
                        "role": {"type": "role", "value": "RootWebArea"},
                        "name": {"type": "name", "value": "Test Page"},
                        "backendDOMNodeId": 1,
                        "childIds": [],
                    }
                ]
            }
        elif method == "DOM.getDocument":
            if self.fail_at == "boxes_document":
                raise RuntimeError("DOM.getDocument failed")
            return {"root": {"nodeId": 1}}
        elif method == "DOM.getBoxModel":
            return {"model": {"border": [0, 0, 100, 0, 100, 100, 0, 100]}}
        elif method == "Accessibility.disable":
            self.release_count += 1
            if self.release_error:
                raise self.release_error
            return {}
        return {}

    def evaluate(self, session_id, expression, timeout=None):
        self.sent_methods.append("Runtime.evaluate")
        if "location.href" in expression:
            if self.fail_at == "url":
                raise RuntimeError("url evaluation failed")
            return json.dumps(["https://example.com", "Test Page"])
        elif "wenderViewport" in expression:
            if self.fail_at == "viewport":
                raise RuntimeError("viewport evaluation failed")
            return json.dumps(
                {
                    "w": 1024,
                    "h": 768,
                    "scroll_x": 0,
                    "scroll_y": 0,
                    "doc_w": 1024,
                    "doc_h": 768,
                    "dpr_ppm": 1000000,
                }
            )
        return json.dumps(None)


class FakeAtspiIndex:
    """Offline fake AT-SPI application index for PID/family-PID resolution tests."""

    def __init__(self, pid_map):
        self.pid_map = pid_map
        self.by_pid = {p: [(f":1.{p}", f"/org/a11y/atspi/accessible/app_{p}")] for p, v in pid_map.items() if v is not None or p in (10, 20, 30, 40, 50, 100, 200)}
        self._cache = {}

    def __bool__(self):
        return bool(self.by_pid)

    def __contains__(self, pid):
        return pid in self.by_pid

    def get(self, pid):
        if pid in self._cache:
            return self._cache[pid]
        if pid not in self.pid_map or self.pid_map[pid] is False:
            return None
        nc = self.pid_map[pid]
        res = {
            "bus": f":1.{pid}",
            "path": f"/org/a11y/atspi/accessible/app_{pid}",
            "pid": pid,
            "name": f"App_{pid}",
            "n_children": nc,
            "toolkit": "GTK",
        }
        self._cache[pid] = res
        return res


class TestPageStateCleanupContracts(unittest.TestCase):
    """Part 1: browser.page_state cleanup contracts after raw_tree acquisition."""

    def test_healthy_control_with_boxes_true(self):
        """Healthy control: successful page_state with boxes=True releases AX exactly once and returns state."""
        fake_cdp = FakeCdp()
        state = browser.page_state(fake_cdp, "s1", with_boxes=True)
        self.assertIsNotNone(state)
        self.assertEqual(state["source"]["kind"], "cdp")
        self.assertEqual(fake_cdp.release_count, 1)

    def test_healthy_control_with_boxes_false(self):
        """Healthy control: successful page_state with boxes=False releases AX exactly once and returns state."""
        fake_cdp = FakeCdp()
        state = browser.page_state(fake_cdp, "s1", with_boxes=False)
        self.assertIsNotNone(state)
        self.assertEqual(state["source"]["kind"], "cdp")
        self.assertEqual(fake_cdp.release_count, 1)

    def test_page_state_cleanup_on_prune_failure(self):
        """fx2-f1-ax-browser-atspi-hardening: pruning failure after raw_tree must release AX exactly once, raise primary error, return no state."""
        fake_cdp = FakeCdp()
        primary_exc = ValueError("pruning error")
        with patch("wender.perceive.ax.prune", side_effect=primary_exc):
            with self.assertRaises(ValueError) as ctx:
                browser.page_state(fake_cdp, "s1", with_boxes=True)
            self.assertIs(ctx.exception, primary_exc)
        self.assertEqual(fake_cdp.release_count, 1)

    def test_page_state_cleanup_on_url_decode_failure(self):
        """fx2-f1-ax-browser-atspi-hardening: URL decoding failure after raw_tree must release AX exactly once, raise primary error, return no state."""
        fake_cdp = FakeCdp(fail_at="url")
        with self.assertRaises(RuntimeError) as ctx:
            browser.page_state(fake_cdp, "s1", with_boxes=True)
        self.assertEqual(str(ctx.exception), "url evaluation failed")
        self.assertEqual(fake_cdp.release_count, 1)

    def test_page_state_cleanup_on_viewport_decode_failure(self):
        """fx2-f1-ax-browser-atspi-hardening: viewport decoding failure after raw_tree must release AX exactly once, raise primary error, return no state."""
        fake_cdp = FakeCdp(fail_at="viewport")
        with self.assertRaises(RuntimeError) as ctx:
            browser.page_state(fake_cdp, "s1", with_boxes=True)
        self.assertEqual(str(ctx.exception), "viewport evaluation failed")
        self.assertEqual(fake_cdp.release_count, 1)

    def test_page_state_cleanup_scope_around_build_state(self):
        """fx2-f1-ax-browser-atspi-hardening: release must occur in finally block AFTER build_state rather than before build_state."""
        fake_cdp = FakeCdp()
        primary_exc = TypeError("build_state error")

        def mock_build_state(*args, **kwargs):
            fake_cdp.release_count_during_build = fake_cdp.release_count
            raise primary_exc

        with patch("wender.state.schema.build_state", side_effect=mock_build_state):
            with self.assertRaises(TypeError) as ctx:
                browser.page_state(fake_cdp, "s1", with_boxes=True)
            self.assertIs(ctx.exception, primary_exc)
        # Unhardened code releases AX BEFORE build_state (so release_count_during_build is 1).
        # In hardened try...finally, build_state runs inside try, so release_count_during_build must be 0.
        self.assertEqual(fake_cdp.release_count_during_build, 0)
        self.assertEqual(fake_cdp.release_count, 1)

    def test_page_state_preserves_primary_error_when_cleanup_fails(self):
        """fx2-f1-ax-browser-atspi-hardening: when page_state fails and ax.release also raises, primary error identity is preserved."""
        fake_cdp = FakeCdp(fail_at="url", release_error=RuntimeError("ax.disable error"))
        with self.assertRaises(RuntimeError) as ctx:
            browser.page_state(fake_cdp, "s1", with_boxes=True)
        self.assertEqual(str(ctx.exception), "url evaluation failed")
        self.assertEqual(fake_cdp.release_count, 1)


class TestAtspiFamilyPidAndTreeSemanticsContracts(unittest.TestCase):
    """Part 2: AT-SPI family-PID fallback, link metadata and tree/rung semantics contracts."""

    def test_window_summary_healthy_controls(self):
        """Healthy controls for window_summary: positive child count (rung 2) and zero child count (rung 4)."""
        idx = FakeAtspiIndex({100: 5, 200: 0})
        s1 = atspi.window_summary(100, idx)
        self.assertTrue(s1["available"])
        self.assertEqual(s1["link"], "by_pid")
        self.assertIs(s1["tree"], True)
        self.assertEqual(s1["rung"], 2)

        s2 = atspi.window_summary(200, idx)
        self.assertTrue(s2["available"])
        self.assertEqual(s2["link"], "by_pid")
        self.assertIs(s2["tree"], False)
        self.assertEqual(s2["rung"], 4)

    def test_window_summary_no_pid_and_no_match(self):
        """Healthy controls for no_pid and no_match links."""
        idx = FakeAtspiIndex({100: 3})
        s_no_pid = atspi.window_summary(None, idx)
        self.assertEqual(s_no_pid["link"], "no_pid")
        self.assertIsNone(s_no_pid["tree"])

        s_no_match = atspi.window_summary(999, idx)
        self.assertEqual(s_no_match["link"], "no_match")
        self.assertIsNone(s_no_match["tree"])

    def test_window_summary_family_pid_fallback_healthy(self):
        """Healthy control for family_pid fallback when direct PID is unindexed but family PID matches."""
        idx = FakeAtspiIndex({100: 4})
        s = atspi.window_summary(70, idx, family_pids=[100])
        self.assertTrue(s["available"])
        self.assertEqual(s["link"], "by_family_pid")
        self.assertEqual(s["pid_atspi"], 100)
        self.assertIs(s["tree"], True)
        self.assertEqual(s["rung"], 2)

    def test_window_summary_unreadable_childcount_none_semantics(self):
        """fx2-f1-ax-browser-atspi-hardening: n_children is None must report tree=None (unknown), rejecting boolean-childcount mutant."""
        idx = FakeAtspiIndex({10: None})
        s = atspi.window_summary(10, idx)
        self.assertTrue(s["available"])
        self.assertIsNone(s["n_children"])
        self.assertIsNone(s["tree"])

    def test_window_summary_negative_childcount_semantics(self):
        """fx2-f1-ax-browser-atspi-hardening: negative n_children (e.g. -1) must not be classified as tree=True or rung=2."""
        idx = FakeAtspiIndex({20: -1})
        s = atspi.window_summary(20, idx)
        self.assertTrue(s["available"])
        self.assertEqual(s["n_children"], -1)
        self.assertNotEqual(s["tree"], True)
        self.assertNotEqual(s.get("rung"), 2)


if __name__ == "__main__":
    unittest.main()
