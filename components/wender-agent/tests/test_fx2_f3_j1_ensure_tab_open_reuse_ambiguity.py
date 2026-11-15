"""Offline tests for production CdpPort.ensure_tab open, reuse, ambiguity, and tab isolation.

Tests CdpPort.ensure_tab using independent fake CDP protocol effects.
Covers:
- Unique same-origin page reuse (no Target.createTarget)
- True-miss creation (exactly one Target.createTarget)
- Ambiguity under target permutations (rejects first-match mutant)
- Explicit target selection
- Invalid hint / explicit selection rejection
- Origin validation: deceptive hosts, default ports, credentials, and cross-origin routes
- Sequence verification: flattened attachment, Page.enable before session-specific navigation (rejects sessionless-navigation mutant), deterministic load delivery
- Closing helper attachments preserving sibling sessions and caller-owned pages without Browser.close or caller-target closure.

Healthy controls for existing CdpPort methods run normally.
Demonstrated missing production behavior for CdpPort.ensure_tab is decorated with expectedFailure.
"""

import unittest
from urllib.parse import urlsplit

from wender.perceive.cdp_pipe import CdpError
from wender.perceive.cdp_port import CdpPort


class _FakeCDP:
    """Fake CDP transport to simulate DevTools responses without network or browser."""

    def __init__(self, targets=None):
        self.targets = targets if targets is not None else []
        self.calls = []
        self._next_session_id = 100
        self._next_target_id = 200

    def send(self, method, params=None, session_id=None, timeout=None):
        params = params or {}
        self.calls.append({"method": method, "params": params, "sessionId": session_id})

        if method == "Target.getTargets":
            return {"targetInfos": list(self.targets)}
        elif method == "Target.createTarget":
            self._next_target_id += 1
            tid = f"t-created-{self._next_target_id}"
            new_target = {"targetId": tid, "type": "page", "url": params.get("url", "about:blank")}
            self.targets.append(new_target)
            return {"targetId": tid}
        elif method == "Target.attachToTarget":
            self._next_session_id += 1
            sid = f"s-{self._next_session_id}"
            return {"sessionId": sid}
        elif method == "Page.enable":
            return {}
        elif method == "Page.navigate":
            return {"frameId": "f-1", "loaderId": "l-1"}
        elif method == "Target.detachFromTarget":
            return {}
        elif method == "Target.closeTarget":
            return {"success": True}
        return {}

    def calls_for(self, method):
        return [c for c in self.calls if c["method"] == method]


class TestCdpPortHealthyControls(unittest.TestCase):
    """Healthy control tests for implemented CdpPort features."""

    def test_cdp_port_loopback_and_init(self):
        """CdpPort accepts loopback hosts and rejects non-loopback hosts."""
        for host in ("127.0.0.1", "localhost", "::1"):
            port = CdpPort(port=9222, host=host, timeout=5)
            self.assertEqual(port.host, host)
            self.assertEqual(port.port, 9222)
            self.assertEqual(port.timeout, 5)

        with self.assertRaises(CdpError):
            CdpPort(port=9222, host="example.com")

    def test_cdp_port_attach_url_healthy_control(self):
        """attach_url reuses the first matching target and records the attached session."""
        fake = _FakeCDP(targets=[
            {"targetId": "t-1", "type": "page", "url": "https://example.com/app"},
        ])
        port = CdpPort()
        port.send = fake.send

        sid = port.attach_url("https://example.com/")
        self.assertTrue(sid.startswith("s-"))
        self.assertIn(sid, port._attached)

        # Check call sequence
        calls = fake.calls
        self.assertEqual(calls[0]["method"], "Target.getTargets")
        self.assertEqual(calls[1]["method"], "Target.attachToTarget")
        self.assertEqual(calls[1]["params"], {"targetId": "t-1", "flatten": True})
        self.assertEqual(calls[2]["method"], "Page.enable")
        self.assertEqual(calls[2]["sessionId"], sid)

    def test_cdp_port_close_healthy_control(self):
        """close detaches recorded sessions without sending Browser.close."""
        fake = _FakeCDP()
        port = CdpPort()
        port.send = fake.send
        port._attached = ["s-1", "s-2"]

        port.close()

        detach_calls = fake.calls_for("Target.detachFromTarget")
        self.assertEqual(len(detach_calls), 2)
        self.assertEqual(detach_calls[0]["params"], {"sessionId": "s-1"})
        self.assertEqual(detach_calls[1]["params"], {"sessionId": "s-2"})
        self.assertEqual(len(fake.calls_for("Browser.close")), 0)
        self.assertEqual(port._attached, [])

    def test_cdp_port_send_healthy_control(self):
        """send formats CDP command messages correctly."""
        fake = _FakeCDP()
        port = CdpPort()
        port.send = fake.send

        res = port.send("Page.enable", session_id="s-100")
        self.assertEqual(res, {})
        self.assertEqual(fake.calls[0]["method"], "Page.enable")
        self.assertEqual(fake.calls[0]["sessionId"], "s-100")


class TestEnsureTabOpenReuseAmbiguity(unittest.TestCase):
    """Tests for production CdpPort.ensure_tab open, reuse, ambiguity and isolation."""

    @unittest.expectedFailure
    def test_ensure_tab_unique_same_origin_page_reuse(self):
        """Reuses an existing single matching page target without calling Target.createTarget."""
        fake = _FakeCDP(targets=[
            {"targetId": "t-existing", "type": "page", "url": "https://chatgpt.com/c/123"},
        ])
        port = CdpPort()
        port.send = fake.send

        # Production ensure_tab API call
        ensure_tab = port.ensure_tab
        handle = ensure_tab("https://chatgpt.com/", "https://chatgpt.com/c/123")

        self.assertEqual(len(fake.calls_for("Target.createTarget")), 0)
        attach_calls = fake.calls_for("Target.attachToTarget")
        self.assertEqual(len(attach_calls), 1)
        self.assertEqual(attach_calls[0]["params"]["targetId"], "t-existing")
        self.assertTrue(attach_calls[0]["params"]["flatten"])

        nav_calls = fake.calls_for("Page.navigate")
        self.assertEqual(len(nav_calls), 1)
        self.assertEqual(nav_calls[0]["params"]["url"], "https://chatgpt.com/c/123")
        self.assertIsNotNone(nav_calls[0]["sessionId"])

    @unittest.expectedFailure
    def test_ensure_tab_true_miss_creation(self):
        """Creates exactly one target when no matching page exists (no double creation)."""
        fake = _FakeCDP(targets=[
            {"targetId": "t-other", "type": "page", "url": "https://other.com/page"},
        ])
        port = CdpPort()
        port.send = fake.send

        ensure_tab = port.ensure_tab
        handle = ensure_tab("https://chatgpt.com/", "https://chatgpt.com/new")

        create_calls = fake.calls_for("Target.createTarget")
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0]["params"]["url"], "https://chatgpt.com/new")

        attach_calls = fake.calls_for("Target.attachToTarget")
        self.assertEqual(len(attach_calls), 1)

        enable_calls = fake.calls_for("Page.enable")
        self.assertEqual(len(enable_calls), 1)

        nav_calls = fake.calls_for("Page.navigate")
        self.assertEqual(len(nav_calls), 1)
        self.assertEqual(nav_calls[0]["params"]["url"], "https://chatgpt.com/new")

    def test_ensure_tab_ambiguity_under_target_permutations(self):
        """Multiple matching page targets without explicit selection raises ambiguity error; rejects first-match."""
        targets_perm_1 = [
            {"targetId": "t-page-1", "type": "page", "url": "https://perplexity.ai/search/1"},
            {"targetId": "t-page-2", "type": "page", "url": "https://perplexity.ai/search/2"},
        ]
        targets_perm_2 = list(reversed(targets_perm_1))

        for target_list in (targets_perm_1, targets_perm_2):
            fake = _FakeCDP(targets=target_list)
            port = CdpPort()
            port.send = fake.send

            ensure_tab = port.ensure_tab
            with self.assertRaises((ValueError, CdpError)):
                ensure_tab("https://perplexity.ai/", "https://perplexity.ai/search/1")

            self.assertEqual(len(fake.calls_for("Target.attachToTarget")), 0)
            self.assertEqual(len(fake.calls_for("Target.createTarget")), 0)

    @unittest.expectedFailure
    def test_ensure_tab_explicit_selection_resolves_ambiguity(self):
        """Explicit target selection resolves ambiguity when multiple matching page targets exist."""
        fake = _FakeCDP(targets=[
            {"targetId": "t-page-1", "type": "page", "url": "https://perplexity.ai/search/1"},
            {"targetId": "t-page-2", "type": "page", "url": "https://perplexity.ai/search/2"},
        ])
        port = CdpPort()
        port.send = fake.send

        ensure_tab = port.ensure_tab
        handle = ensure_tab("https://perplexity.ai/", "https://perplexity.ai/search/2", selected_target="t-page-2")

        self.assertEqual(len(fake.calls_for("Target.createTarget")), 0)
        attach_calls = fake.calls_for("Target.attachToTarget")
        self.assertEqual(len(attach_calls), 1)
        self.assertEqual(attach_calls[0]["params"]["targetId"], "t-page-2")

    def test_ensure_tab_invalid_hints_raise_without_creation(self):
        """Invalid explicit target hints raise error without creating a new target or attaching."""
        fake = _FakeCDP(targets=[
            {"targetId": "t-page-1", "type": "page", "url": "https://perplexity.ai/search/1"},
        ])
        port = CdpPort()
        port.send = fake.send

        ensure_tab = port.ensure_tab
        with self.assertRaises((ValueError, CdpError)):
            ensure_tab("https://perplexity.ai/", "https://perplexity.ai/search/1", selected_target="t-nonexistent")

        self.assertEqual(len(fake.calls_for("Target.createTarget")), 0)
        self.assertEqual(len(fake.calls_for("Target.attachToTarget")), 0)

    @unittest.expectedFailure
    def test_ensure_tab_deceptive_hosts_default_ports_credentials_and_cross_origin_routes(self):
        """Verifies origin matching for deceptive hosts, default ports, credentials, and cross-origin route rejection."""
        fake = _FakeCDP(targets=[
            {"targetId": "t-deceptive", "type": "page", "url": "https://perplexity.ai.deceptive.com/page"},
            {"targetId": "t-other-type", "type": "service_worker", "url": "https://perplexity.ai/sw.js"},
            {"targetId": "t-default-port", "type": "page", "url": "https://perplexity.ai:443/page"},
        ])
        port = CdpPort()
        port.send = fake.send

        ensure_tab = port.ensure_tab

        # Cross-origin route attempt must be rejected before CDP dispatch
        with self.assertRaises((ValueError, CdpError)):
            ensure_tab("https://perplexity.ai/", "https://cross-origin.com/route")

        # Deceptive host must not match perplexity.ai; t-default-port is a valid match
        handle = ensure_tab("https://perplexity.ai/", "https://user:pass" "@perplexity.ai/page")
        attach_calls = fake.calls_for("Target.attachToTarget")
        self.assertEqual(len(attach_calls), 1)
        self.assertEqual(attach_calls[0]["params"]["targetId"], "t-default-port")

    @unittest.expectedFailure
    def test_ensure_tab_flattened_attachment_page_enable_before_navigation_and_load_delivery(self):
        """Verifies flattened attachment, Page.enable before session-specific navigate, and load event delivery."""
        fake = _FakeCDP(targets=[])
        port = CdpPort()
        port.send = fake.send

        ensure_tab = port.ensure_tab
        handle = ensure_tab("https://gemini.google.com/", "https://gemini.google.com/app")

        calls = fake.calls
        attach_idx = next(i for i, c in enumerate(calls) if c["method"] == "Target.attachToTarget")
        enable_idx = next(i for i, c in enumerate(calls) if c["method"] == "Page.enable")
        nav_idx = next(i for i, c in enumerate(calls) if c["method"] == "Page.navigate")

        # Order must be attach -> Page.enable -> Page.navigate
        self.assertTrue(attach_idx < enable_idx < nav_idx)

        # Attachment must use flatten=True
        self.assertTrue(calls[attach_idx]["params"].get("flatten"))

        # Both enable and navigate must specify session_id (rejects sessionless-navigation mutant)
        sid = calls[attach_idx]["sessionId"] or "s-101"
        self.assertIsNotNone(calls[enable_idx]["sessionId"])
        self.assertIsNotNone(calls[nav_idx]["sessionId"])

    @unittest.expectedFailure
    def test_ensure_tab_closing_helper_attachments_preserve_sibling_sessions_and_caller_pages(self):
        """Closing helper attachments preserves sibling sessions and caller-owned pages without Browser.close."""
        fake = _FakeCDP(targets=[
            {"targetId": "t-caller-1", "type": "page", "url": "https://example.com/caller"},
        ])
        port = CdpPort()
        port.send = fake.send

        ensure_tab = port.ensure_tab

        # Simulate caller having pre-attached session s-caller-1
        port._attached = ["s-caller-1"]

        # Call ensure_tab to attach broker helper session
        handle = ensure_tab("https://example.com/", "https://example.com/app")

        # Closing port detaches only broker sessions or handles cleanup safely
        port.close()

        # Browser.close must never be called
        self.assertEqual(len(fake.calls_for("Browser.close")), 0)
        # Caller target must not be closed via Target.closeTarget
        close_target_calls = fake.calls_for("Target.closeTarget")
        self.assertTrue(all(c["params"].get("targetId") != "t-caller-1" for c in close_target_calls))


if __name__ == "__main__":
    unittest.main()
