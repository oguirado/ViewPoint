"""Adversarial claim-pinning tests for explore stop-state handling (WP1-05)."""
import hashlib
import unittest

from wender.learn.explore import Driver, explore
from wender.state.schema import build_state


def _make_state(url, screen_name="sorry_screen"):
    sha = hashlib.sha256(screen_name.encode("utf-8")).hexdigest()
    stable_sha = hashlib.sha256((screen_name + "_stable").encode("utf-8")).hexdigest()
    node = {
        "id": hashlib.sha256(screen_name.encode("utf-8")).hexdigest()[:16],
        "role": "button",
        "name": "Submit",
        "depth": 0,
        "ordinal": 0,
        "attrs": [],
    }
    return build_state(
        [node],
        kind="cdp",
        url=url,
        title="Google Sorry / Unverifiable",
        ax_sha256=sha,
        ax_stable_sha256=stable_sha,
        prune={"name": "prune_ax_v1", "version": 1},
        raw_count=10,
        actionable_roles=frozenset(["button"]),
    )


class StopStateDriver(Driver):
    """Driver that simulates a STOP state page (e.g. Google sorry page)."""

    def __init__(self, url="https://www.google.com/sorry/index"):
        self.url = url
        self.state = _make_state(url)
        self.screen_fp = "1111222233334444"
        self.act_called = False

    def observe(self):
        return self.state, self.screen_fp

    def candidate_actions(self, state):
        return [
            {"op": "click", "target": "submit_button", "irreversible": False}
        ]

    def act(self, action):
        self.act_called = True
        raise AssertionError("act must not be called on a STOP state")


class HealthyDriver(Driver):
    """Control driver that simulates a healthy page."""

    def __init__(self):
        self.state = _make_state("https://www.google.com/search")
        self.screen_fp = "5555666677778888"
        self.act_called = False

    def observe(self):
        return self.state, self.screen_fp

    def candidate_actions(self, state):
        return [
            {"op": "click", "target": "search_button", "irreversible": False},
            {"op": "click", "target": "filter_button", "irreversible": False},
        ]

    def act(self, action):
        self.act_called = True


class TestJQ1A6StopExplore(unittest.TestCase):
    def test_healthy_control_explore_runs(self):
        """Healthy control: normal page exploration completes without triggering stopstate."""
        driver = HealthyDriver()
        res = explore(driver, "google_ai", max_steps=2)
        self.assertTrue(driver.act_called)
        self.assertEqual(len(res["steps"]), 2)
        self.assertEqual(res["stopped"], "budget")

    # Pending real detector from fx2-f1-perceive-wait-text-stopstate.
    def test_explore_halts_on_stop_state(self):
        """WP1-05: wender.learn.explore.explore checks stopstate.detect on observed states and halts immediately.

        The guard is wired; fx2-f1-perceive-wait-text-stopstate must supply the
        production detector before this integration pin can be promoted.
        """
        # Missing-module imports inside the decorated test method so test discovery succeeds
        try:
            from wender.perceive import stopstate
        except ImportError:
            stopstate = None

        driver = StopStateDriver(url="https://www.google.com/sorry/index")
        res = explore(driver, "google_ai", max_steps=10)

        self.assertEqual(len(res["steps"]), 0)
        self.assertFalse(driver.act_called)
        self.assertIn(res["stopped"], ("stopstate", "stop_state", "stopped"))


if __name__ == "__main__":
    unittest.main()
