"""Test suite for ChromeDriver actionable roles, candidate actions, and execution seams.

Task: fx2-f2-j5-chromedriver-actionable-roles
Fix: fx2-f2-executor-actor-chromedriver
"""
import random
import unittest

from wender.act import cdp_executor
from wender.learn.chrome_driver import ChromeDriver
from wender.perceive import ax
from wender.state import schema


class FakeCDP:
    """In-memory stateful FakeCDP adapter for ChromeDriver.act test seams."""

    def __init__(self, raw_nodes=None, fail_on_method=None):
        self.raw_nodes = raw_nodes or []
        self.enabled = False
        self.calls = []
        self.fail_on_method = fail_on_method

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})

        if method == self.fail_on_method:
            raise RuntimeError("Simulated failure for method %s" % method)

        if method == "Accessibility.enable":
            self.enabled = True
            return {}
        elif method == "Accessibility.disable":
            self.enabled = False
            return {}
        elif method == "Accessibility.getFullAXTree":
            return {"nodes": self.raw_nodes}
        elif method == "DOM.scrollIntoViewIfNeeded":
            return {}
        elif method == "DOM.resolveNode":
            bid = payload.get("backendNodeId")
            return {"object": {"objectId": "obj-%s" % bid}}
        elif method == "Runtime.callFunctionOn":
            return {"result": {"type": "undefined"}}
        raise ValueError("Unsupported CDP method in FakeCDP: %s" % method)


def _build_raw_tree_with_roles(role_specs):
    """Helper to construct raw AX tree from list of (node_id, role, name, properties, backend_id)."""
    child_ids = [spec[0] for spec in role_specs]
    root = {
        "nodeId": "1",
        "ignored": False,
        "role": {"type": "role", "value": "RootWebArea"},
        "name": {"type": "computedString", "value": "Test Page"},
        "childIds": child_ids,
    }
    nodes = [root]
    for spec in role_specs:
        node_id, role, name, props, backend_id = spec
        node = {
            "nodeId": node_id,
            "ignored": False,
            "role": {"type": "role", "value": role},
            "name": {"type": "computedString", "value": name},
            "childIds": [],
        }
        if props:
            node["properties"] = props
        if backend_id is not None:
            node["backendDOMNodeId"] = backend_id
        nodes.append(node)
    return nodes


def _build_state_from_raw(raw_nodes):
    """Prune raw nodes and build validated STATE object."""
    pruned = ax.prune(raw_nodes)
    boxes = {n["id"]: [10, 10 + i * 30, 100, 25] for i, n in enumerate(pruned)}
    return schema.build_state(
        pruned,
        kind="cdp",
        url="file:///test.html",
        title="Test Page",
        ax_sha256="a" * 64,
        ax_stable_sha256="b" * 64,
        prune={"name": "prune_ax_v1", "version": 1},
        raw_count=len(raw_nodes),
        boxes=boxes,
        viewport={
            "w": 1024,
            "h": 768,
            "scroll_x": 0,
            "scroll_y": 0,
            "doc_w": 1024,
            "doc_h": 768,
            "dpr_ppm": 1000000,
        },
        actionable_roles=ax.ACTIONABLE_ROLES,
    )


class TestChromeDriverActionableRoles(unittest.TestCase):
    """Test candidate action extraction and execution seams for ChromeDriver."""

    def test_link_and_button_candidate_actions(self):
        """Ordinary passing test: link and button roles generate candidate actions."""
        role_specs = [
            ("2", "button", "Submit Form", None, 102),
            ("3", "link", "Go Home", None, 103),
            ("4", "heading", "Page Header", None, 104),
        ]
        raw_nodes = _build_raw_tree_with_roles(role_specs)
        state = _build_state_from_raw(raw_nodes)

        driver = ChromeDriver(None, None, None)
        actions = driver.candidate_actions(state)

        # Retrieve target IDs for button and link nodes
        button_node = next(n for n in state["nodes"] if n["role"] == "button")
        link_node = next(n for n in state["nodes"] if n["role"] == "link")
        heading_node = next(n for n in state["nodes"] if n["role"] == "heading")

        action_targets = {a["target"] for a in actions}

        self.assertIn(button_node["id"], action_targets)
        self.assertIn(link_node["id"], action_targets)
        self.assertNotIn(heading_node["id"], action_targets)
        self.assertEqual(len(actions), 2)

    def test_all_actionable_roles_coverage_starvation(self):
        """Expected failure: candidate_actions drops non-link/button actionable roles.

        Fix: fx2-f2-executor-actor-chromedriver
        Currently wender/learn/chrome_driver.py filters role in ("link", "button"),
        starving candidate actions for the rest of ax.ACTIONABLE_ROLES.
        """
        all_roles = sorted(list(ax.ACTIONABLE_ROLES))
        # Shuffle roles and interleave nonactionable nodes
        rng = random.Random(42)
        permuted_roles = list(all_roles)
        rng.shuffle(permuted_roles)

        role_specs = []
        for i, role in enumerate(permuted_roles):
            role_specs.append(("%d" % (i + 2), role, "Control %s" % role, None, 200 + i))
            # Interleave nonactionable node every 3 items
            if i % 3 == 0:
                role_specs.append(("%d_na" % (i + 2), "paragraph", "Text %d" % i, None, 500 + i))

        raw_nodes = _build_raw_tree_with_roles(role_specs)
        state = _build_state_from_raw(raw_nodes)

        driver = ChromeDriver(None, None, None)
        actions = driver.candidate_actions(state)

        action_targets = {a["target"] for a in actions}
        actionable_state_nodes = [n for n in state["nodes"] if n.get("actionable")]

        # Validate that state building correctly marked all 16 actionable role nodes as actionable
        self.assertEqual(len(actionable_state_nodes), len(all_roles))

        # This assertion fails on current ChromeDriver because candidate_actions checks
        # if role in ("link", "button"), omitting 14 other actionable roles.
        for node in actionable_state_nodes:
            self.assertIn(
                node["id"],
                action_targets,
                "candidate_actions must include actionable %s node (target %s)"
                % (node["role"], node["id"]),
            )

    def test_disabled_states_excluded_from_candidate_actions(self):
        """Passing test: disabled controls have actionable==False and yield no candidate actions."""
        disabled_prop = [{"name": "disabled", "value": {"type": "value", "value": True}}]
        role_specs = [
            ("2", "button", "Active Button", None, 102),
            ("3", "button", "Disabled Button", disabled_prop, 103),
            ("4", "link", "Active Link", None, 104),
            ("5", "link", "Disabled Link", disabled_prop, 105),
        ]
        raw_nodes = _build_raw_tree_with_roles(role_specs)
        state = _build_state_from_raw(raw_nodes)

        disabled_button = next(n for n in state["nodes"] if n["name"] == "Disabled Button")
        disabled_link = next(n for n in state["nodes"] if n["name"] == "Disabled Link")

        self.assertFalse(disabled_button["actionable"])
        self.assertFalse(disabled_link["actionable"])

        driver = ChromeDriver(None, None, None)
        actions = driver.candidate_actions(state)
        action_targets = {a["target"] for a in actions}

        self.assertNotIn(disabled_button["id"], action_targets)
        self.assertNotIn(disabled_link["id"], action_targets)

    def test_duplicate_names_handled_independently(self):
        """Passing test: controls with duplicate names produce distinct candidate action targets."""
        role_specs = [
            ("2", "button", "Delete", None, 102),
            ("3", "button", "Delete", None, 103),
            ("4", "link", "More Info", None, 104),
            ("5", "link", "More Info", None, 105),
        ]
        raw_nodes = _build_raw_tree_with_roles(role_specs)
        state = _build_state_from_raw(raw_nodes)

        driver = ChromeDriver(None, None, None)
        actions = driver.candidate_actions(state)

        self.assertEqual(len(actions), 4)

        target_ids = [a["target"] for a in actions]
        self.assertEqual(len(set(target_ids)), 4, "Every candidate action must have a unique target ID")

    def test_irreversible_name_classification(self):
        """Passing test: candidate actions correctly classify irreversible keywords in target names."""
        role_specs = [
            ("2", "button", "Delete Account", None, 102),
            ("3", "button", "REMOVE ITEM", None, 103),
            ("4", "button", "Pay Invoice Now", None, 104),
            ("5", "button", "Save Draft", None, 105),
            ("6", "link", "Cancel Order", None, 106),
        ]
        raw_nodes = _build_raw_tree_with_roles(role_specs)
        state = _build_state_from_raw(raw_nodes)

        driver = ChromeDriver(None, None, None)
        actions = driver.candidate_actions(state)

        actions_by_target = {a["target"]: a for a in actions}

        delete_btn = next(n for n in state["nodes"] if n["name"] == "Delete Account")
        remove_btn = next(n for n in state["nodes"] if n["name"] == "REMOVE ITEM")
        pay_btn = next(n for n in state["nodes"] if n["name"] == "Pay Invoice Now")
        save_btn = next(n for n in state["nodes"] if n["name"] == "Save Draft")
        cancel_link = next(n for n in state["nodes"] if n["name"] == "Cancel Order")

        self.assertTrue(actions_by_target[delete_btn["id"]]["irreversible"])
        self.assertTrue(actions_by_target[remove_btn["id"]]["irreversible"])
        self.assertTrue(actions_by_target[pay_btn["id"]]["irreversible"])
        self.assertFalse(actions_by_target[save_btn["id"]]["irreversible"])
        self.assertFalse(actions_by_target[cancel_link["id"]]["irreversible"])

    def test_act_returns_none_on_verified_success(self):
        """Passing test: ChromeDriver.act returns None on verified action success."""
        role_specs = [("2", "button", "Confirm", None, 301)]
        raw_nodes = _build_raw_tree_with_roles(role_specs)
        state = _build_state_from_raw(raw_nodes)

        target_id = state["nodes"][1]["id"]
        action = {"op": "click", "target": target_id}

        cdp = FakeCDP(raw_nodes)
        driver = ChromeDriver(cdp, "session-123", "http://example.com")

        result = driver.act(action)
        self.assertIsNone(result, "ChromeDriver.act must return None on verified success")

        # Verify backend ID passed to DOM CDP methods
        scroll_call = next(c for c in cdp.calls if c["method"] == "DOM.scrollIntoViewIfNeeded")
        resolve_call = next(c for c in cdp.calls if c["method"] == "DOM.resolveNode")

        self.assertEqual(scroll_call["params"]["backendNodeId"], 301)
        self.assertEqual(resolve_call["params"]["backendNodeId"], 301)
        self.assertEqual(scroll_call["session_id"], "session-123")

    def test_act_cleanup_and_ax_release_on_failure(self):
        """Passing test: ChromeDriver.act executes AX release cleanup even when errors occur."""
        role_specs = [("2", "button", "Trigger Error", None, 401)]
        raw_nodes = _build_raw_tree_with_roles(role_specs)

        cdp = FakeCDP(raw_nodes)
        driver = ChromeDriver(cdp, "session-456", "http://example.com")

        # Act on non-existent target ID
        action = {"op": "click", "target": "non_existent_id"}
        with self.assertRaises(cdp_executor.ActionError):
            driver.act(action)

        # Confirm Accessibility.disable (ax.release) was called during cleanup
        disable_calls = [c for c in cdp.calls if c["method"] == "Accessibility.disable"]
        self.assertTrue(len(disable_calls) > 0, "ax.release (Accessibility.disable) must be called")

    def test_act_action_error_propagation(self):
        """Passing test: ChromeDriver.act propagates ActionError when receipt ok is falsy."""
        role_specs = [("2", "button", "Failing Button", None, 501)]
        raw_nodes = _build_raw_tree_with_roles(role_specs)
        state = _build_state_from_raw(raw_nodes)

        target_id = state["nodes"][1]["id"]
        action = {"op": "click", "target": target_id}

        # Simulate Runtime.callFunctionOn failure in FakeCDP
        cdp = FakeCDP(raw_nodes, fail_on_method="Runtime.callFunctionOn")
        driver = ChromeDriver(cdp, "session-789", "http://example.com")

        with self.assertRaises(cdp_executor.ActionError) as ctx:
            driver.act(action)

        self.assertIn("Action failed:", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
