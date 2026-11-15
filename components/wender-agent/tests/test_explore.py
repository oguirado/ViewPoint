"""Unit tests for wender.learn.explore."""
import hashlib
import unittest

from wender.learn.explore import Driver, explore
from wender.remember.program_map import state_key
from wender.state.schema import build_state


def _dummy_state(screen_name, raw_count=10):
    sha = hashlib.sha256(screen_name.encode("utf-8")).hexdigest()
    stable_sha = hashlib.sha256((screen_name + "_stable").encode("utf-8")).hexdigest()
    node = {
        "id": hashlib.sha256(screen_name.encode("utf-8")).hexdigest()[:16],
        "role": "button",
        "name": screen_name + " button",
        "depth": 0,
        "ordinal": 0,
        "attrs": [],
    }
    return build_state(
        [node],
        kind="cdp",
        url="http://example.com/" + screen_name,
        title=screen_name.capitalize(),
        ax_sha256=sha,
        ax_stable_sha256=stable_sha,
        prune={"name": "prune_ax_v1", "version": 1},
        raw_count=raw_count,
        actionable_roles=frozenset(["button"]),
    )


class FakeSiteDriver(Driver):
    def __init__(self, raise_on_target=None):
        self.raise_on_target = raise_on_target
        # Screens: home, list, detail, settings, dead_end
        self.states = {
            "home": _dummy_state("home", 10),
            "list": _dummy_state("list", 20),
            "detail": _dummy_state("detail", 30),
            "settings": _dummy_state("settings", 40),
            "dead_end": _dummy_state("dead_end", 50),
        }
        self.screen_fps = {
            "home": "1111111111111111",
            "list": "2222222222222222",
            "detail": "3333333333333333",
            "settings": "4444444444444444",
            "dead_end": "5555555555555555",
        }
        self.current_screen = "home"
        self.reset_count = 0

    def observe(self):
        return self.states[self.current_screen], self.screen_fps[self.current_screen]

    def candidate_actions(self, state):
        cur = self.current_screen
        if cur == "home":
            return [
                {"op": "click", "target": "go_list", "irreversible": False},
                {"op": "click", "target": "go_settings", "irreversible": False},
                {"op": "click", "target": "no_op", "irreversible": False},
            ]
        elif cur == "list":
            return [
                {"op": "click", "target": "go_detail", "irreversible": False},
                {"op": "click", "target": "delete_all", "irreversible": True},
            ]
        elif cur == "detail":
            return [
                {"op": "click", "target": "go_dead_end", "irreversible": False},
                {"op": "click", "target": "go_home", "irreversible": False},  # cycle back to home
            ]
        elif cur == "settings":
            return [
                {"op": "click", "target": "go_home", "irreversible": False},
            ]
        elif cur == "dead_end":
            return []
        return []

    def act(self, action):
        target = action.get("target")
        if target == self.raise_on_target:
            raise RuntimeError("Driver failure on " + target)

        if target == "go_list":
            self.current_screen = "list"
        elif target == "go_settings":
            self.current_screen = "settings"
        elif target == "no_op":
            pass  # state does not change
        elif target == "go_detail":
            self.current_screen = "detail"
        elif target == "go_dead_end":
            self.current_screen = "dead_end"
        elif target == "go_home":
            self.current_screen = "home"

    def reset(self):
        self.reset_count += 1
        self.current_screen = "home"


class TestExplore(unittest.TestCase):
    def test_full_exploration(self):
        driver = FakeSiteDriver()
        res = explore(driver, "test_app", max_steps=32, reset=driver.reset)

        self.assertEqual(res["stopped"], "exhausted")
        self.assertGreater(driver.reset_count, 0)

        # Check nodes
        pm = res["program_map"]
        self.assertEqual(len(pm.nodes), 5)  # home, list, detail, settings, dead_end

        # Check irreversible action skipped
        skipped_targets = [a["target"] for a in res["skipped"]]
        self.assertIn("delete_all", skipped_targets)

        # Check no-op edge exists where pre == post
        no_op_steps = [s for s in res["steps"] if s["action"]["target"] == "no_op"]
        self.assertTrue(len(no_op_steps) > 0)
        self.assertEqual(no_op_steps[0]["pre"], no_op_steps[0]["post"])

        # Check changed summary format on edges
        for s in res["steps"]:
            self.assertTrue(s["changed"].startswith("plus "))

    def test_budget_stop(self):
        driver = FakeSiteDriver()
        res = explore(driver, "test_app", max_steps=2, reset=driver.reset)
        self.assertEqual(res["stopped"], "budget")
        self.assertEqual(len(res["steps"]), 2)

    def test_error_stop(self):
        driver = FakeSiteDriver(raise_on_target="go_detail")
        res = explore(driver, "test_app", max_steps=32, reset=driver.reset)
        self.assertEqual(res["stopped"], "error")

        # Map is partial
        pm = res["program_map"]
        self.assertIn(state_key("1111111111111111", driver.states["home"]["ax_stable_sha256"]), pm.nodes)

    def test_determinism(self):
        driver1 = FakeSiteDriver()
        res1 = explore(driver1, "test_app", max_steps=32, reset=driver1.reset)

        driver2 = FakeSiteDriver()
        res2 = explore(driver2, "test_app", max_steps=32, reset=driver2.reset)

        json1 = res1["program_map"].to_json()
        json2 = res2["program_map"].to_json()
        self.assertEqual(json1, json2)


if __name__ == "__main__":
    unittest.main()
