import unittest

class TestNavigateIsolation(unittest.TestCase):

    def test_navigate_preserves_concurrent_session_load_events(self):
        """Defect S1-F4: CdpPipe.navigate(session_id, url) purges only the requested session's

        stale Page.loadEventFired events, preserving concurrent tab session events.
        """
        from wender.perceive.cdp_pipe import CdpPipe

        c = CdpPipe(chrome_bin="/bin/true")
        c.events.append({"method": "Page.loadEventFired", "sessionId": "sid_A", "params": {}})
        c.events.append({"method": "Page.loadEventFired", "sessionId": "sid_B", "params": {}})

        def fake_send(method, params=None, session_id=None, timeout=None):
            if method == "Page.navigate":
                c.events.append({"method": "Page.loadEventFired", "sessionId": "sid_A", "params": {}})
                return {"frameId": "f1"}
            return {}

        c.send = fake_send
        c.navigate("sid_A", "https://example.com", timeout=0.1)

        ev_b = c.wait_event("Page.loadEventFired", session_id="sid_B", timeout=0.1)
        self.assertEqual(ev_b.get("sessionId"), "sid_B")

    def test_navigate_healthy_control_single_session(self):
        """Healthy control: Single session navigation successfully completes without failure."""
        from wender.perceive.cdp_pipe import CdpPipe

        c = CdpPipe(chrome_bin="/bin/true")

        def fake_send(method, params=None, session_id=None, timeout=None):
            if method == "Page.navigate":
                c.events.append({"method": "Page.loadEventFired", "sessionId": "sid_A", "params": {}})
                return {"frameId": "f1"}
            return {}

        c.send = fake_send
        res = c.navigate("sid_A", "https://example.com", timeout=0.1)
        self.assertEqual(res, {"frameId": "f1"})


if __name__ == "__main__":
    unittest.main()
