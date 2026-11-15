"""Adversarial claim-pinning tests for wender.perceive.browser.page_state AX release cleanup.

Claim: wender.perceive.browser.page_state(cdp, session_id) always calls ax.release(cdp, session_id)
(Accessibility.disable) in a finally block even when cdp.evaluate or DOM.getDocument raises an exception after ax.raw_tree.

Known defect: defect S1-F5 (wender/perceive/browser.py:46-53 calls ax.release without try ... finally,
docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:107,279).
"""
import json
import unittest
from unittest import mock


class FakeCdp:
    """Offline fake CDP transport recording sent methods and simulating responses or errors."""
    def __init__(self, evaluate_error=None, get_document_error=None, disable_error=None):
        self.sent_methods = []
        self.evaluate_error = evaluate_error
        self.get_document_error = get_document_error
        self.disable_error = disable_error

    def send(self, method, params=None, session_id=None):
        self.sent_methods.append(method)
        if method == "Accessibility.enable":
            return {}
        elif method == "Accessibility.getFullAXTree":
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
            if self.get_document_error:
                raise self.get_document_error
            return {"root": {"nodeId": 1}}
        elif method == "DOM.getBoxModel":
            return {"model": {"border": [0, 0, 100, 0, 100, 100, 0, 100]}}
        elif method == "Accessibility.disable":
            if self.disable_error:
                raise self.disable_error
            return {}
        return {}

    def evaluate(self, session_id, expression, timeout=None):
        self.sent_methods.append("Runtime.evaluate")
        if self.evaluate_error:
            raise self.evaluate_error
        if "location.href" in expression:
            return json.dumps(["https://example.com", "Test Page"])
        elif "wenderViewport" in expression:
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


class TestJq1A6AxRelease(unittest.TestCase):

    def test_page_state_ax_release_on_evaluate_error(self):
        # Defect S1-F5: page_state calls ax.release without try...finally,
        # so an exception in cdp.evaluate after raw_tree bypasses ax.release.
        from wender.perceive import browser
        from wender.perceive.cdp_pipe import CdpError

        fake_cdp = FakeCdp(evaluate_error=CdpError("evaluate failed"))
        with self.assertRaises(CdpError):
            browser.page_state(fake_cdp, "s1")

        self.assertIn("Accessibility.disable", fake_cdp.sent_methods)

    def test_page_state_ax_release_on_get_document_error(self):
        # Defect S1-F5: page_state calls ax.release without try...finally,
        # so an exception in DOM.getDocument after raw_tree bypasses ax.release.
        from wender.perceive import browser
        from wender.perceive.cdp_pipe import CdpError

        fake_cdp = FakeCdp(get_document_error=CdpError("getDocument failed"))
        with self.assertRaises(CdpError):
            browser.page_state(fake_cdp, "s1")

        self.assertIn("Accessibility.disable", fake_cdp.sent_methods)

    def test_page_state_ax_release_healthy_control(self):
        """Healthy control: successful page_state execution releases AX."""
        from wender.perceive import browser

        fake_cdp = FakeCdp()
        state = browser.page_state(fake_cdp, "s1")

        self.assertIsNotNone(state)
        self.assertEqual(state["source"]["kind"], "cdp")
        self.assertIn("Accessibility.disable", fake_cdp.sent_methods)

    def test_release_once_for_other_construction_faults(self):
        from wender.perceive import browser

        for target, attribute in ((browser.ax, "prune"), (browser, "inpage_script"),
                                  (browser, "boxes"), (browser.schema, "build_state")):
            with self.subTest(stage=attribute):
                fake_cdp = FakeCdp()
                original = RuntimeError(attribute + " failed")
                with mock.patch.object(target, attribute, side_effect=original), \
                        mock.patch.object(browser.ax, "release", wraps=browser.ax.release) as release:
                    with self.assertRaises(RuntimeError) as caught:
                        browser.page_state(fake_cdp, "s1")
                self.assertIs(caught.exception, original)
                release.assert_called_once_with(fake_cdp, "s1")
                self.assertEqual(fake_cdp.sent_methods.count("Accessibility.disable"), 1)

    def test_release_once_for_metadata_and_viewport_decode_faults(self):
        from wender.perceive import browser

        metadata = json.dumps(["https://example.com", "Test Page"])
        cases = (("metadata_json", ["{"], json.JSONDecodeError),
                 ("metadata_shape", ["[]"], ValueError),
                 ("viewport_json", [metadata, "{"], json.JSONDecodeError),
                 ("viewport_evaluate", [metadata, RuntimeError("viewport failed")], RuntimeError))
        for stage, replies, error_type in cases:
            with self.subTest(stage=stage):
                fake_cdp = FakeCdp()
                with mock.patch.object(fake_cdp, "evaluate", side_effect=replies):
                    with self.assertRaises(error_type):
                        browser.page_state(fake_cdp, "s1")
                self.assertEqual(fake_cdp.sent_methods.count("Accessibility.disable"), 1)

    def test_state_build_precedes_release_and_no_boxes_path_is_preserved(self):
        from wender.perceive import browser

        fake_cdp = FakeCdp()
        build_state = browser.schema.build_state

        def build_before_release(*args, **kwargs):
            self.assertNotIn("Accessibility.disable", fake_cdp.sent_methods)
            return build_state(*args, **kwargs)

        with mock.patch.object(browser.schema, "build_state", side_effect=build_before_release):
            state = browser.page_state(fake_cdp, "s1", with_boxes=False)
        self.assertEqual(state["source"]["kind"], "cdp")
        self.assertTrue(all(node["box"] is None for node in state["nodes"]))
        self.assertNotIn("DOM.getDocument", fake_cdp.sent_methods)
        self.assertNotIn("DOM.getBoxModel", fake_cdp.sent_methods)
        self.assertEqual(fake_cdp.sent_methods.count("Accessibility.disable"), 1)

    def test_original_failure_wins_when_release_also_raises(self):
        from wender.perceive import browser

        for original in (RuntimeError("evaluate failed"), KeyboardInterrupt("interrupted")):
            with self.subTest(error_type=type(original).__name__):
                fake_cdp = FakeCdp(evaluate_error=original)
                with mock.patch.object(browser.ax, "release", side_effect=RuntimeError("cleanup failed")) as release:
                    with self.assertRaises(type(original)) as caught:
                        browser.page_state(fake_cdp, "s1")
                self.assertIs(caught.exception, original)
                release.assert_called_once_with(fake_cdp, "s1")

    def test_cleanup_only_exception_propagates(self):
        from wender.perceive import browser

        fake_cdp = FakeCdp()
        cleanup_error = RuntimeError("cleanup failed")
        with mock.patch.object(browser.ax, "release", side_effect=cleanup_error) as release:
            with self.assertRaises(RuntimeError) as caught:
                browser.page_state(fake_cdp, "s1")
        self.assertIs(caught.exception, cleanup_error)
        release.assert_called_once_with(fake_cdp, "s1")

    def test_release_false_retains_existing_best_effort_contract(self):
        from wender.perceive import browser

        fake_cdp = FakeCdp(disable_error=RuntimeError("disable failed"))
        state = browser.page_state(fake_cdp, "s1")
        self.assertEqual(state["source"]["kind"], "cdp")
        self.assertEqual(fake_cdp.sent_methods.count("Accessibility.disable"), 1)

    def test_no_release_without_successful_acquisition(self):
        from wender.perceive import browser

        fake_cdp = FakeCdp()
        original = RuntimeError("raw tree failed")
        with mock.patch.object(browser.ax, "raw_tree", side_effect=original), \
                mock.patch.object(browser.ax, "release") as release:
            with self.assertRaises(RuntimeError) as caught:
                browser.page_state(fake_cdp, "s1")
        self.assertIs(caught.exception, original)
        release.assert_not_called()
        self.assertEqual(fake_cdp.sent_methods, [])


if __name__ == "__main__":
    unittest.main()
