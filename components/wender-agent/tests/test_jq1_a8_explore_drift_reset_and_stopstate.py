"""Route-control unit tests for D-A8-12 in explore: drift, reset, and stopstate."""
import hashlib
import unittest
from unittest.mock import patch

from wender.learn.explore import Driver, explore
from wender.remember.program_map import state_key
from wender.state.schema import build_state


def _make_state(screen_name, raw_count=10, url=None, title=None, heading_name=None):
    sha = hashlib.sha256(screen_name.encode("utf-8")).hexdigest()
    stable_sha = hashlib.sha256((screen_name + "_stable").encode("utf-8")).hexdigest()
    node_name = heading_name if heading_name else (screen_name + " button")
    node = {
        "id": hashlib.sha256(screen_name.encode("utf-8")).hexdigest()[:16],
        "role": "heading" if heading_name else "button",
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


class NoneCandidateDriver(Driver):
    def __init__(self):
        self.state = _make_state("initial")
        self.act_count = 0

    def observe(self):
        return self.state, "1111111111111111"

    def candidate_actions(self, state):
        return None

    def act(self, action):
        self.act_count += 1


class DriftRouteDriver(Driver):
    def __init__(self):
        self.states = {
            "A": _make_state("screen_A"),
            "B": _make_state("screen_B"),
            "C": _make_state("screen_C"),
            "D": _make_state("screen_D"),
        }
        self.screen_fps = {
            "A": "aaaaaaaaaaaaaaaa",
            "B": "bbbbbbbbbbbbbbbb",
            "C": "cccccccccccccccc",
            "D": "dddddddddddddddd",
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
        elif cur == "D":
            return []
        return []

    def act(self, action):
        target = action.get("target")
        self.act_history.append((self.current, target))
        if target == "go_B":
            self.go_b_count += 1
            if self.go_b_count == 1:
                self.current = "B"
            else:
                # On 2nd traversal from A, drift to D instead of B
                self.current = "D"
        elif target == "go_C":
            self.current = "C"
        elif target == "go_A":
            self.current = "A"
        elif target == "C_untried":
            pass


class ConsecutiveDeadEndDriver(Driver):
    def __init__(self):
        self.state = _make_state("dead_end")
        self.reset_count = 0
        self.act_count = 0

    def observe(self):
        return self.state, "9999999999999999"

    def candidate_actions(self, state):
        return []

    def act(self, action):
        self.act_count += 1

    def reset(self):
        self.reset_count += 1


class StopstateDriver(Driver):
    def __init__(self):
        self.state = _make_state(
            "sorry",
            url="http://example.com/sorry/index",
            title="Unusual traffic",
            heading_name="Our systems have detected unusual traffic from your computer network.",
        )
        self.act_count = 0

    def observe(self):
        return self.state, "0000000000000000"

    def candidate_actions(self, state):
        return [{"op": "click", "target": "retry", "irreversible": False}]

    def act(self, action):
        self.act_count += 1


class TestExploreDriftResetAndStopstate(unittest.TestCase):
    def test_candidate_actions_none_exhausts_without_dispatch(self):
        driver = NoneCandidateDriver()
        res = explore(driver, "test_app", max_steps=10)
        self.assertEqual(res["stopped"], "exhausted")
        self.assertEqual(res["steps"], [])
        self.assertEqual(driver.act_count, 0)

    def test_route_replay_drift_bypasses_expected_hop_from_drifted_state(self):
        driver = DriftRouteDriver()
        res = explore(driver, "test_app", max_steps=10)

        # Confirm step trace shows drift to D on second go_B dispatch from A
        steps = res["steps"]
        self.assertGreaterEqual(len(steps), 4)

        # Step 1: A -> B (go_B)
        self.assertEqual(steps[0]["action"]["target"], "go_B")
        self.assertEqual(steps[0]["post"], state_key(driver.screen_fps["B"], driver.states["B"]["ax_stable_sha256"]))

        # Step 2: B -> C (go_C)
        self.assertEqual(steps[1]["action"]["target"], "go_C")
        self.assertEqual(steps[1]["post"], state_key(driver.screen_fps["C"], driver.states["C"]["ax_stable_sha256"]))

        # Step 3: C -> A (go_A)
        self.assertEqual(steps[2]["action"]["target"], "go_A")
        self.assertEqual(steps[2]["post"], state_key(driver.screen_fps["A"], driver.states["A"]["ax_stable_sha256"]))

        # Step 4: A -> D (go_B, second time; drifted to D)
        self.assertEqual(steps[3]["action"]["target"], "go_B")
        self.assertEqual(steps[3]["post"], state_key(driver.screen_fps["D"], driver.states["D"]["ax_stable_sha256"]))

        # Prove expected B-to-C hop is never dispatched from D
        actions_after_drift = driver.act_history[4:]
        for state_at_act, target in actions_after_drift:
            self.assertNotEqual(state_at_act, "D")

    def test_consecutive_dead_end_resets_exactly_once(self):
        driver = ConsecutiveDeadEndDriver()
        res = explore(driver, "test_app", max_steps=10, reset=driver.reset)
        self.assertEqual(res["stopped"], "exhausted")
        self.assertEqual(driver.reset_count, 1)

    # Pending real detector from fx2-f1-perceive-wait-text-stopstate.
    def test_stopstate_gating_unusual_traffic(self):
        driver = StopstateDriver()
        res = explore(driver, "test_app", max_steps=10)
        self.assertEqual(res["stopped"], "stopstate")
        self.assertEqual(res["steps"], [])
        self.assertEqual(driver.act_count, 0)


class TestExploreObservationBoundaries(unittest.TestCase):
    """Guard orchestration only; these fakes do not certify the missing F1 detector.

    The original stopstate pins above still exercise the production detector.
    These receipts isolate when explore must latch, including replay and reset.
    """

    @staticmethod
    def clear():
        return {"is_stop": False, "reason": None, "evidence": []}

    @staticmethod
    def wall():
        return {"is_stop": True, "reason": "fixture_wall", "evidence": []}

    def test_initial_stop_precedes_map_candidates_reset_and_dispatch(self):
        driver = StopstateDriver()
        with patch("wender.learn.explore._detect_stopstate", return_value=self.wall()), \
                patch.object(driver, "candidate_actions", wraps=driver.candidate_actions) as candidates, \
                patch.object(driver, "observe", wraps=driver.observe) as observe, \
                patch.object(driver, "act", wraps=driver.act) as act:
            reset = unittest.mock.Mock()
            result = explore(driver, "app", reset=reset)
        self.assertEqual(result["stopped"], "stopstate")
        self.assertEqual(result["program_map"].nodes, {})
        self.assertEqual(result["steps"], [])
        self.assertEqual(observe.call_count, 1)
        candidates.assert_not_called()
        reset.assert_not_called()
        act.assert_not_called()

    def test_post_action_stop_latches_before_candidates_or_next_action(self):
        driver = DriftRouteDriver()
        with patch("wender.learn.explore._detect_stopstate",
                   side_effect=[self.clear(), self.wall()]) as detect, \
                patch.object(driver, "candidate_actions", wraps=driver.candidate_actions) as candidates:
            result = explore(driver, "app", max_steps=1)
        self.assertEqual(result["stopped"], "stopstate")
        self.assertEqual(driver.act_history, [("A", "go_B")])
        self.assertEqual(detect.call_count, 2)
        self.assertEqual(candidates.call_count, 1)
        self.assertEqual(len(result["program_map"].nodes), 1)
        self.assertEqual(result["program_map"].edges, [])
        self.assertEqual(result["steps"], [])

    def test_route_replay_stop_prevents_following_expected_hop(self):
        driver = DriftRouteDriver()
        receipts = [self.clear() for _ in range(4)] + [self.wall()]
        with patch("wender.learn.explore._detect_stopstate", side_effect=receipts) as detect, \
                patch.object(driver, "candidate_actions", wraps=driver.candidate_actions) as candidates:
            result = explore(driver, "app", max_steps=20)
        self.assertEqual(result["stopped"], "stopstate")
        self.assertEqual(driver.act_history,
                         [("A", "go_B"), ("B", "go_C"), ("C", "go_A"), ("A", "go_B")])
        self.assertEqual(detect.call_count, 5)
        self.assertEqual(candidates.call_count, 4)
        self.assertEqual(len(result["steps"]), 3)
        self.assertNotIn(state_key(driver.screen_fps["D"],
                                   driver.states["D"]["ax_stable_sha256"]),
                         result["program_map"].nodes)

    def test_post_reset_stop_precedes_new_candidates_and_second_reset(self):
        driver = ConsecutiveDeadEndDriver()
        with patch("wender.learn.explore._detect_stopstate",
                   side_effect=[self.clear(), self.wall()]) as detect, \
                patch.object(driver, "candidate_actions", wraps=driver.candidate_actions) as candidates:
            result = explore(driver, "app", reset=driver.reset)
        self.assertEqual(result["stopped"], "stopstate")
        self.assertEqual(driver.reset_count, 1)
        self.assertEqual(driver.act_count, 0)
        self.assertEqual(detect.call_count, 2)
        self.assertEqual(candidates.call_count, 1)

    def test_missing_f1_preserves_exploration_with_unavailable_receipt(self):
        # fx2-f1-perceive-wait-text-stopstate supplies F1 independently.
        # This tests the explicit optional-import contract without that module.
        driver = DriftRouteDriver()
        with patch.dict("sys.modules", {"wender.perceive.stopstate": None}), \
                patch.object(driver, "candidate_actions", wraps=driver.candidate_actions) as candidates:
            result = explore(driver, "app", max_steps=2)
        self.assertEqual(result["stopped"], "budget")
        self.assertEqual(result["stopstate"], "unavailable")
        self.assertNotIn("failure", result)
        self.assertEqual(len(result["program_map"].nodes), 3)
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(driver.act_history, [("A", "go_B"), ("B", "go_C")])
        self.assertEqual(candidates.call_count, 3)

    def test_detector_failure_is_not_permission_to_dispatch(self):
        driver = StopstateDriver()
        with patch("wender.learn.explore._detect_stopstate", side_effect=ValueError("invalid receipt")):
            result = explore(driver, "app")
        self.assertEqual(result["stopped"], "error")
        self.assertEqual(result["failure"]["type"], "ValueError")
        self.assertEqual(driver.act_count, 0)

    def test_real_detector_adapter_rejects_truthy_nonboolean_verdicts(self):
        from types import ModuleType
        from wender.learn.explore import _detect_stopstate

        module = ModuleType("wender.perceive.stopstate")
        for invalid in (None, {}, {"is_stop": 1}, {"is_stop": "yes"},
                        {"is_stop": True, "reason": " "}):
            with self.subTest(verdict=invalid):
                module.detect = lambda state, value=invalid: value
                with patch.dict("sys.modules", {module.__name__: module}):
                    with self.assertRaises(ValueError):
                        _detect_stopstate(_make_state("ordinary"))


if __name__ == "__main__":
    unittest.main()
