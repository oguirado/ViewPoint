# @module {"id":"tests.test_browser_isolation_refusals","layer":3,"owner":"TESTS","deps":["wa.perceive.browser"],"book":"HB-WA-s1-perceive"}
"""Browser refusal boundaries, connection scope and cleanup regression tests."""
import unittest
from dataclasses import replace
from unittest.mock import patch

from wender.perceive import browser
from tests.test_jq2a_m8_j7_browser_session_handle_isolation import (
    FakeCdp, FakeSessionRegistry, Handle, TargetRecord,
)


class TestBrowserIsolationRefusals(unittest.TestCase):
    def setUp(self):
        self.cdp = FakeCdp()
        self.a = Handle("cdp_tab", "a", 1)
        self.b = Handle("cdp_tab", "b", 1)
        self.record = TargetRecord(self.a, self.cdp, True, 1, "shared", True, False)
        self.registry = FakeSessionRegistry({
            "a": self.record,
            "b": replace(self.record, handle=self.b),
        })

    def test_boxes_refuses_sibling_before_io(self):
        browser.boxes(self.cdp, handle=self.a, registry=self.registry)
        before = list(self.cdp.sent_calls)
        with self.assertRaises(browser.SharedSessionError):
            browser.boxes(self.cdp, handle=self.b, registry=self.registry)
        self.assertEqual(self.cdp.sent_calls, before)

    def test_session_binding_is_shared_between_registries(self):
        browser.boxes(self.cdp, handle=self.a, registry=self.registry)
        other_registry = FakeSessionRegistry({"b": self.registry.records["b"]})
        with self.assertRaises(browser.SharedSessionError):
            browser.page_state(self.cdp, handle=self.b, registry=other_registry)

    def test_distinct_connections_can_reuse_session_ids(self):
        browser.boxes(self.cdp, handle=self.a, registry=self.registry)
        other_cdp = FakeCdp()
        other_registry = FakeSessionRegistry({
            "b": replace(self.record, handle=self.b, transport=other_cdp),
        })
        state = browser.page_state(other_cdp, handle=self.b, registry=other_registry)
        self.assertEqual(state["source"]["url"], "https://example.com/shared")

    def test_late_focus_claim_releases_ax_with_implicit_registry(self):
        self.cdp.registry = self.registry
        evaluate = self.cdp.evaluate

        def claim_focus(*args, **kwargs):
            self.registry.active_focus_leases.append(object())
            return evaluate(*args, **kwargs)

        with patch.object(self.cdp, "evaluate", side_effect=claim_focus):
            with self.assertRaises(browser.PerceptionFocusError):
                browser.page_state(self.cdp, with_boxes=False, handle=self.a)
        self.assertEqual(self.cdp.released_sessions, ["shared"])

    def test_focus_claim_during_boxes_is_refused(self):
        send = self.cdp.send

        def claim_focus(*args, **kwargs):
            self.registry.active_focus_leases.append(object())
            return send(*args, **kwargs)

        with patch.object(self.cdp, "send", side_effect=claim_focus):
            with self.assertRaises(browser.PerceptionFocusError):
                browser.boxes(self.cdp, handle=self.a, registry=self.registry)
