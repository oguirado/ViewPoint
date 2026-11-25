"""Offline tests for the executor's opt-in browser readiness signal."""
import json
import os
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
from uuid import uuid4

from tests import test_cdp_executor as executor_tests
from tests import test_jq3_r_r7_c1_fix_cdp_executor_lock_and_fresh_gate as authority_tests
from wender.act.cdp_executor import ActionError, run_ax_action
from wender.perceive import ax
from wender.perceive.cdp_pipe import CdpError, CdpPipe
from wender.state.schema import dumps


BINDING = "fixtureReadiness"
SIGNAL = "Runtime.bindingCalled"


def binding_event(run, *, owner=1, name=BINDING):
    return {"params": {"name": name, "payload": json.dumps({"run": run, "owner": owner})}}


class TestCdpReadiness(unittest.TestCase):
    def setUp(self):
        self.before = ax.prune(executor_tests._form_raw())
        self.after = ax.prune(executor_tests._form_raw(btn_name="Sent"))
        self.action = {"op": "click", "target": self.before[2]["id"],
                       "expect": {"target_name": "Sent"}}
        self.cdp = executor_tests.FakeCdp()
        self.cdp.wait_event = Mock(return_value=binding_event("current"))
        self.sleep = Mock()

    def execute(self, read_nodes, **kwargs):
        return run_ax_action(self.cdp, "s1", self.before, self.action, read_nodes,
                             sleep=self.sleep, **kwargs)

    def test_omitted_and_explicit_off_have_identical_receipt_and_sleep(self):
        omitted = self.execute(lambda: self.after)
        calls = list(self.cdp.calls)
        self.cdp.calls.clear()
        explicit = self.execute(lambda: self.after, readiness=None)
        self.assertEqual(dumps(omitted), dumps(explicit))
        self.assertEqual(calls, self.cdp.calls)
        self.assertEqual([call.args for call in self.sleep.call_args_list], [(0.25,), (0.25,)])
        self.cdp.wait_event.assert_not_called()

    def test_matching_event_ends_wait_and_fresh_ax_controls_verdict(self):
        read = Mock(return_value=self.after)
        receipt = self.execute(read, readiness=(BINDING, "current"))
        self.assertEqual((receipt["ok"], receipt["verified"], receipt["attempts"]), (1, 1, 1))
        self.cdp.wait_event.assert_called_once_with(SIGNAL, session_id="s1", timeout=0.25)
        self.sleep.assert_not_called()
        read.assert_called_once_with()

    def test_expiry_matches_fixed_wait_after_ignoring_foreign_and_invalid_events(self):
        for after in (self.before, self.after):
            with self.subTest(after=after[2]["name"]):
                fixed = self.execute(lambda: after, retries=1)
                self.cdp.wait_event.reset_mock()
                self.cdp.wait_event.side_effect = [
                    binding_event("stale"),
                    binding_event("current", owner=2),
                    binding_event("current", owner=True),
                    binding_event("current", name="otherBinding"),
                    {"params": {"name": BINDING, "payload": "malformed"}},
                    CdpError("timeout waiting for event " + SIGNAL),
                ]
                with patch("wender.act.cdp_executor.time.monotonic",
                           side_effect=[0, .03125, .0625, .09375, .125, .15625]):
                    ready = self.execute(lambda: after, retries=1, readiness=(BINDING, "current"))
                self.assertEqual(dumps(fixed), dumps(ready))
                self.assertEqual([c.kwargs["timeout"] for c in self.cdp.wait_event.call_args_list],
                                 [.25, .21875, .1875, .15625, .125, .09375])

    def test_early_signal_cannot_trigger_redispatch_or_credit(self):
        read = Mock(side_effect=[self.before, self.after])
        receipt = self.execute(read, retries=3, allow_coordinates=True,
                               readiness=(BINDING, "current"))
        self.assertEqual((receipt["ok"], receipt["verified"], receipt["attempts"]), (0, 0, 1))
        self.assertEqual(receipt["error"], "expectation not satisfied")
        self.assertEqual([c["method"] for c in self.cdp.calls],
                         ["DOM.scrollIntoViewIfNeeded", "DOM.resolveNode", "Runtime.callFunctionOn"])
        read.assert_called_once_with()

    def test_lost_action_reply_cannot_trigger_redispatch(self):
        self.cdp.scripted["Runtime.callFunctionOn"] = [CdpError("lost action reply")]
        read = Mock(return_value=self.after)
        receipt = self.execute(read, allow_coordinates=True, readiness=(BINDING, "current"))
        self.assertEqual((receipt["ok"], receipt["verified"], receipt["attempts"]), (0, 0, 1))
        self.assertEqual(receipt["error"], "lost action reply")
        self.cdp.wait_event.assert_not_called()
        read.assert_not_called()

    def test_coordinate_fallback_waits_after_ax_resolution_failure(self):
        self.cdp.scripted["DOM.resolveNode"] = [{"object": None}]
        read = Mock(side_effect=[self.before, self.after])
        receipt = self.execute(read, retries=1, allow_coordinates=True,
                               readiness=(BINDING, "current"))
        self.assertEqual((receipt["path"], receipt["verified"], receipt["attempts"]),
                         ("coordinates", 1, 2))
        self.assertEqual([c["params"]["type"] for c in self.cdp.calls
                          if c["method"] == "Input.dispatchMouseEvent"],
                         ["mouseMoved", "mousePressed", "mouseReleased"])
        self.cdp.wait_event.assert_called_once()
        self.sleep.assert_not_called()

    def test_missing_or_already_satisfied_expectation_refuses_before_send(self):
        for expect in (None, {"target_name": self.before[2]["name"]}):
            with self.subTest(expect=expect):
                self.action["expect"] = expect
                with self.assertRaisesRegex(ActionError, "unmet AX expectation"):
                    self.execute(lambda: self.before, readiness=(BINDING, "current"))
        self.assertEqual(self.cdp.calls, [])

    def test_authorized_ax_and_coordinates_leave_verdict_to_readback(self):
        for path in ("ax", "coordinates"):
            for applied in (False, True):
                with self.subTest(path=path, applied=applied):
                    case = authority_tests.Case()
                    case.cdp.wait_event = Mock(return_value=binding_event("current"))
                    snapshots = iter(([case.node], [case.after if applied else case.node]))
                    result = run_ax_action(
                        case.cdp, "s1", [case.node], dict(case.action, path=path),
                        lambda: next(snapshots), allow_coordinates=True,
                        lease=case.lease, focus=case.focus, observation=case.observation,
                        registry=case.registry, focus_policy=case.policy,
                        writer_fence=case.fence, clock=lambda: case.now,
                        readiness=(BINDING, "current"),
                    )
                    self.assertEqual((result["path"], result["ok"], result["verified"],
                                      result["attempts"]), (path, int(applied), int(applied), 1))
                    case.cdp.wait_event.assert_called_once()
                    self.assertEqual(case.fence.releases, 1)


@unittest.skipUnless(os.environ.get("CHROME_BIN"), "CHROME_BIN required for local fixture replay")
class TestCdpReadinessBrowser(unittest.TestCase):
    @staticmethod
    def page_url(scenario, token):
        page = """<!doctype html><meta charset="utf-8">
<h1 id="heading">Ready to submit</h1>
<button id="submit" type="button">Submit fixture</button>
<script>
const [scenario, run] = SCENARIO_AND_TOKEN;
const button = document.querySelector("#submit");
const heading = document.querySelector("#heading");
const owners = [];
const signal = owner => window.fixtureReadiness(JSON.stringify({run, owner}));
function apply() {
    heading.textContent = "Submission confirmed";
    if (scenario !== "heading-only") button.textContent = "Fixture submitted";
}
button.addEventListener("click", () => {
    const owner = owners.length + 1;
    owners.push(owner);
    switch (scenario) {
        case "retry":
            if (owner === 1) signal(owner);
            else setTimeout(() => { apply(); signal(1); }, 65);
            break;
        case "early":
            signal(owner);
            setTimeout(apply, 600);
            break;
        case "no-effect":
            signal(owner);
            break;
        default:
            setTimeout(() => { apply(); signal(owner); }, 65);
    }
});
window.fixtureSnapshot = () => ({run, owners, heading: heading.textContent});
</script>"""
        return "data:text/html;charset=utf-8," + quote(
            page.replace("SCENARIO_AND_TOKEN", json.dumps([scenario, token]))
        )

    def test_fixture_cases_with_product_api(self):
        with TemporaryDirectory(dir="/dev/shm") as home, patch.dict(os.environ, {"HOME": home}):
            with CdpPipe(timeout=3) as cdp:
                session = cdp.attach_page()
                cdp.send("Runtime.enable", session_id=session)
                cdp.send("Runtime.addBinding", {"name": BINDING}, session_id=session)
                cdp.send("Accessibility.enable", session_id=session)

                def read_ax():
                    return ax.prune(cdp.send("Accessibility.getFullAXTree",
                                             session_id=session)["nodes"])

                for scenario in ("nominal", "heading-only", "no-effect", "early", "retry"):
                    for enabled in (False, True):
                        with self.subTest(scenario=scenario, readiness=enabled):
                            token = uuid4().hex
                            cdp.navigate(session, self.page_url(scenario, token))
                            nodes = read_ax()
                            button, = [node for node in nodes if node["role"] == "button"]
                            result = run_ax_action(
                                cdp, session, nodes,
                                {"op": "click", "target": button["id"],
                                 "expect": {"target_name": "Fixture submitted"}},
                                read_ax, retries=3 if enabled else 1,
                                readiness=(BINDING, token) if enabled else None,
                            )
                            expected = int(scenario == "nominal")
                            self.assertEqual((result["ok"], result["verified"], result["attempts"]),
                                             (expected, expected, 1))
                            app = cdp.evaluate(session, "window.fixtureSnapshot()")
                            self.assertEqual(app["run"], token)
                            self.assertEqual(app["owners"], [1])
                            if scenario in ("nominal", "heading-only"):
                                self.assertEqual(app["heading"], "Submission confirmed")
