"""Unit tests for explore stopstate detection during post-action observation, route replay, and reset recovery."""
import hashlib
import unittest

from wender.learn.explore import Driver, explore
from wender.state.schema import build_state


def _make_state(screen_name, raw_count=10, url=None, title=None, heading_name=None):
    sha = hashlib.sha256(screen_name.encode("utf-8")).hexdigest()
    stable_sha = hashlib.sha256((screen_name + "_stable").encode("utf-8")).hexdigest()
    node_name = heading_name if heading_name else (screen_name + " button")
    node = {
        "id": hashlib.sha256(screen_name.encode("utf-8")).hexdigest()[:16],
        "role": "button",
        "name": node_name,
        "depth": 0,
        "ordinal": 0,
        "attrs": [],
    }
    return build_state(
        [node],
        kind="cdp",
        url=url or ("http://example.com/" + screen_name),
        title=title or screen_name.capitalize(),
        ax_sha256=sha,
        ax_stable_sha256=stable_sha,
        prune={"name": "prune_ax_v1", "version": 1},
        raw_count=raw_count,
        actionable_roles=frozenset(["button"]),
    )


class PostActionStopDriver(Driver):
    def __init__(self):
        self.states = {
            "home": _make_state("home", url="http://example.com/home"),
            "sorry": _make_state(
                "sorry",
                url="https://www.google.com/sorry/index",
                title="Unusual traffic",
                heading_name="Unusual traffic detected",
            ),
        }
        self.screen_fps = {
            "home": "1111111111111111",
            "sorry": "0000000000000000",
        }
        self.current = "home"
        self.act_count = 0

    def observe(self):
        return self.states[self.current], self.screen_fps[self.current]

    def candidate_actions(self, state):
        if self.current == "home":
            return [{"op": "click", "target": "go_sorry", "irreversible": False}]
        elif self.current == "sorry":
            return [{"op": "click", "target": "retry", "irreversible": False}]
        return []

    def act(self, action):
        self.act_count += 1
        if action.get("target") == "go_sorry":
            self.current = "sorry"


class RouteReplayStopDriver(Driver):
    def __init__(self):
        self.states = {
            "A": _make_state("screen_A", url="http://example.com/A"),
            "B": _make_state("screen_B", url="http://example.com/B"),
            "C": _make_state("screen_C", url="http://example.com/C"),
            "B_sorry": _make_state(
                "sorry",
                url="https://www.google.com/sorry/index?continue=http://example.com/B",
                title="Unusual traffic",
                heading_name="Unusual traffic detected",
            ),
        }
        self.screen_fps = {
            "A": "aaaaaaaaaaaaaaaa",
            "B": "bbbbbbbbbbbbbbbb",
            "C": "cccccccccccccccc",
            "B_sorry": "9999999999999999",
        }
        self.current = "A"
        self.go_b_count = 0
        self.act_history = []

    def observe(self):
        return self.states[self.current], self.screen_fps[self.current]

    def candidate_actions(self, state):
        cur = self.current
        if cur == "A":
            return [{"op": "click", "target": "go_B", "irreversible": False}]
        elif cur == "B":
            return [{"op": "click", "target": "go_C", "irreversible": False}]
        elif cur == "C":
            return [
                {"op": "click", "target": "go_A", "irreversible": False},
                {"op": "click", "target": "C_untried", "irreversible": False},
            ]
        elif cur == "B_sorry":
            return [{"op": "click", "target": "retry", "irreversible": False}]
        return []

    def act(self, action):
        target = action.get("target")
        self.act_history.append((self.current, target))
        if target == "go_B":
            self.go_b_count += 1
            if self.go_b_count == 1:
                self.current = "B"
            else:
                # During route replay, B has become a STOP page
                self.current = "B_sorry"
        elif target == "go_C":
            self.current = "C"
        elif target == "go_A":
            self.current = "A"
        elif target == "C_untried":
            pass


class ResetRecoveryStopDriver(Driver):
    def __init__(self):
        self.states = {
            "dead_end": _make_state("dead_end", url="http://example.com/dead_end"),
            "sorry": _make_state(
                "sorry",
                url="https://www.google.com/sorry/index",
                title="Unusual traffic",
                heading_name="Unusual traffic detected",
            ),
        }
        self.screen_fps = {
            "dead_end": "1111222233334444",
            "sorry": "0000000000000000",
        }
        self.current = "dead_end"
        self.act_count = 0
        self.reset_count = 0

    def observe(self):
        return self.states[self.current], self.screen_fps[self.current]

    def candidate_actions(self, state):
        if self.current == "dead_end":
            return []
        elif self.current == "sorry":
            return [{"op": "click", "target": "retry", "irreversible": False}]
        return []

    def act(self, action):
        self.act_count += 1

    def reset(self):
        self.reset_count += 1
        self.current = "sorry"


class HealthyNearMissDriver(Driver):
    def __init__(self):
        self.states = {
            "home": _make_state("home", url="http://example.com/home"),
            "sorry_info": _make_state(
                "sorry_info",
                url="http://example.com/sorry_info",
                title="Sorry for the wait - Maintenance notice",
                heading_name="Apology and status updates",
            ),
            "done": _make_state("done", url="http://example.com/done"),
        }
        self.screen_fps = {
            "home": "1111111111111111",
            "sorry_info": "5555555555555555",
            "done": "6666666666666666",
        }
        self.current = "home"
        self.act_count = 0

    def observe(self):
        return self.states[self.current], self.screen_fps[self.current]

    def candidate_actions(self, state):
        if self.current == "home":
            return [{"op": "click", "target": "go_info", "irreversible": False}]
        elif self.current == "sorry_info":
            return [{"op": "click", "target": "proceed", "irreversible": False}]
        elif self.current == "done":
            return []
        return []

    def act(self, action):
        self.act_count += 1
        target = action.get("target")
        if target == "go_info":
            self.current = "sorry_info"
        elif target == "proceed":
            self.current = "done"


class BoundedHealthyDriver(Driver):
    def __init__(self):
        self.states = {
            "A": _make_state("screen_A", url="http://example.com/A"),
            "B": _make_state("screen_B", url="http://example.com/B"),
            "C": _make_state("screen_C", url="http://example.com/C"),
        }
        self.screen_fps = {
            "A": "aaaaaaaaaaaaaaaa",
            "B": "bbbbbbbbbbbbbbbb",
            "C": "cccccccccccccccc",
        }
        self.current = "A"
        self.act_count = 0
        self.reset_count = 0

    def observe(self):
        return self.states[self.current], self.screen_fps[self.current]

    def candidate_actions(self, state):
        cur = self.current
        if cur == "A":
            return [
                {"op": "click", "target": "go_B", "irreversible": False},
                {"op": "click", "target": "go_C", "irreversible": False},
            ]
        elif cur == "B":
            return [{"op": "click", "target": "go_A", "irreversible": False}]
        elif cur == "C":
            return []
        return []

    def act(self, action):
        self.act_count += 1
        target = action.get("target")
        if target == "go_B":
            self.current = "B"
        elif target == "go_C":
            self.current = "C"
        elif target == "go_A":
            self.current = "A"

    def reset(self):
        self.reset_count += 1
        self.current = "A"


class TestExploreStopstateAndDriftRecovery(unittest.TestCase):
    @unittest.expectedFailure
    def test_stopstate_first_appears_post_action_observation(self):
        try:
            from wender.perceive import stopstate  # noqa: F401
        except ImportError:
            pass

        driver = PostActionStopDriver()
        res = explore(driver, "test_app", max_steps=10)

        self.assertEqual(res["stopped"], "stopstate")
        self.assertEqual(len(res["steps"]), 1)
        self.assertEqual(driver.act_count, 1)

    @unittest.expectedFailure
    def test_stopstate_first_appears_during_route_replay(self):
        try:
            from wender.perceive import stopstate  # noqa: F401
        except ImportError:
            pass

        driver = RouteReplayStopDriver()
        res = explore(driver, "test_app", max_steps=10)

        self.assertEqual(res["stopped"], "stopstate")
        self.assertEqual(len(res["steps"]), 4)
        for state_at_act, _ in driver.act_history[4:]:
            self.assertNotEqual(state_at_act, "B_sorry")

    def test_stopstate_first_appears_during_reset_recovery(self):
        try:
            from wender.perceive import stopstate  # noqa: F401
        except ImportError:
            pass

        driver = ResetRecoveryStopDriver()
        res = explore(driver, "test_app", max_steps=10, reset=driver.reset)

        self.assertEqual(res["stopped"], "stopstate")
        self.assertEqual(len(res["steps"]), 0)
        self.assertEqual(driver.act_count, 0)
        self.assertEqual(driver.reset_count, 1)

    def test_healthy_near_miss_does_not_trigger_stopstate(self):
        driver = HealthyNearMissDriver()
        res = explore(driver, "test_app", max_steps=10)

        self.assertEqual(res["stopped"], "exhausted")
        self.assertEqual(len(res["steps"]), 2)
        self.assertEqual(driver.act_count, 2)

    def test_bounded_healthy_route_adheres_to_max_steps(self):
        driver = BoundedHealthyDriver()
        res = explore(driver, "test_app", max_steps=3, reset=driver.reset)

        self.assertEqual(res["stopped"], "budget")
        self.assertEqual(len(res["steps"]), 3)
        self.assertEqual(driver.act_count, 3)


if __name__ == "__main__":
    unittest.main()
