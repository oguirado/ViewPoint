from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic_ns
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

DIRECT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(DIRECT), str(DIRECT.parents[1] / "agents")]

from bus.journal import Journal
from channel.vision import VisionFallback
from human import FocusWitness, FreshObservation, HandoffBroker, HandoffRefused
from session.registry import SessionRegistry
from window import WindowBinding
from xpra import XpraHandoff, launch_rootless


def example(name):
    return json.loads((DIRECT / "schemas/tests/examples" / f"{name}_v1.valid.json").read_bytes())


class FakeXpra:
    def __init__(self, binding):
        self.source = (binding,)
        self.visible = ()
        self.server_mode = "start"
        self.events = []

    def start(self, *, mode, display, app, home):
        self.events.append(("start", mode, display, app, home))

    def mode(self, display):
        return self.server_mode

    def windows(self, display):
        return self.source

    def forwarded(self, display):
        return self.visible

    def attach(self, binding):
        self.events.append(("attach", binding))
        self.visible = (binding,)

    def detach(self, binding):
        self.events.append(("detach", binding))
        self.visible = ()


class FixtureRuntime:
    """Private vision stand-in on the real S4 runtime port."""

    def __init__(self):
        self.connected = ("vision",)

    def last_observation_id(self):
        return "before-take"

    def reconcile(self, lock):
        raise AssertionError("the fixture has no unresolved acts")

    def detach(self):
        self.connected = ()

    def attachments(self):
        return self.connected

    def retain(self, session, binding):
        return dict(schema="vp.perception_state/v1", session_id=session["session_id"],
                    surface_id=binding["surface_id"], incarnation=binding["incarnation"],
                    authority_epoch=binding["authority_epoch"], state="ASLEEP",
                    stream_generation=1, obligations=[], since_mono_ns=monotonic_ns())

    def reconnect(self, mode, session):
        self.connected = ("vision",)

    def reauthorize(self, session, binding):
        return session["lease"]["authority_epoch"] == binding["authority_epoch"]

    def observe(self, session, binding):
        return FreshObservation(session["session_id"], binding["surface_id"],
                                binding["incarnation"], binding["authority_epoch"],
                                "after-return", monotonic_ns(), 2)


class XpraTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.session = dict(example("vp.session"), display="xpra:47", browser="firefox",
                            backend="vision", cdp_allowed=False, cdp_port=None)
        del self.session["cdp_target_id"]
        self.sid = self.session["session_id"]
        self.binding = WindowBinding(0x123, self.session["window"]["pid"],
                                     self.session["window"]["witness"],
                                     self.session["incarnation"], self.session["display"])
        self.runner = FakeXpra(self.binding)
        self.registry = SessionRegistry({}, None, data_home=self.root / "registry")
        self.registry.register_attached(self.session)
        surface = dict(example("vp.surface_binding"), incarnation=self.binding.incarnation,
                       owner=self.session["lease"]["holder"])
        self.broker = HandoffBroker(self.root / "rt")
        self.addCleanup(self.broker.close)
        self.broker.bind(self.sid, surface, lease_state="AGENT")
        self.runtime = FixtureRuntime()
        focus = Mock(spec=["save_focus", "restore_focus", "verify_focus"])
        focus.save_focus.return_value = FocusWitness(0x456, 456, "operator-witness", 0)
        self.handoff = XpraHandoff(runner=self.runner, focus=focus, registry=self.registry,
                                   broker=self.broker, session_id=self.sid,
                                   surface_id=surface["surface_id"], runtime=self.runtime,
                                   lineage=Journal(self.root))

    def call(self, op, *, caller="operator", dry_run=False):
        args = {"session_id": self.sid}
        if op == "vp_human_take":
            args["reason"] = "operator"
        context = dict(caller=caller, agent_instance=(self.session["lease"]["holder"]
                                                       if caller == "agent" else "operator"),
                       session_id=self.sid, incarnation=self.binding.incarnation,
                       authority_epoch=self.broker.lease(self.sid)["authority_epoch"])
        return self.handoff.execute(dict(schema="vp.control.req/v1", request_id=uuid4().hex,
                                         op=op, context=context, args=args, dry_run=dry_run))

    def assert_cycle(self):
        before = self.runner.windows(self.binding.display)
        self.assertEqual(before, (self.binding,))
        self.assertEqual(self.call("vp_human_take").code, "OK")
        self.assertEqual(self.runner.forwarded(self.binding.display), before)
        self.assertEqual(self.broker.lease(self.sid)["state"], "HUMAN")
        self.assertEqual(self.runtime.attachments(), ())
        self.assertEqual(self.call("vp_human_return").code, "OK")
        self.assertEqual(self.runner.forwarded(self.binding.display), ())
        self.assertEqual(self.runner.windows(self.binding.display), before)
        self.assertEqual(self.broker.lease(self.sid)["state"], "AGENT")
        self.assertEqual(self.broker.returned_observation(self.sid).observation_id, "after-return")
        self.assertEqual(self.runner.events, [("attach", self.binding), ("detach", self.binding)])

    def test_s4_cycle_preserves_pid_and_forwards_one_window(self):
        self.assert_cycle()

    def test_attach_outside_s4_refuses_even_with_broker_fence(self):
        with self.assertRaisesRegex(HandoffRefused, "NO_LEASE"):
            self.handoff.windows.summon(self.handoff.get_session(self.sid))
        self.broker.fence(self.sid, 1, "operator", {})
        with self.assertRaisesRegex(HandoffRefused, "NO_LEASE"):
            self.handoff.windows.summon(self.handoff.get_session(self.sid))
        self.assertEqual(self.runner.events, [])

    def test_agent_and_preview_cannot_attach(self):
        self.assertEqual(self.call("vp_human_take", caller="agent").code, "NO_LEASE")
        self.assertEqual(self.call("vp_human_take", dry_run=True).code, "OK")
        self.assertEqual(self.runner.events, [])
        self.assertEqual(self.broker.lease(self.sid)["state"], "AGENT")

    def launch(self, **overrides):
        options = dict(display=self.binding.display, app="firefox",
                       private_home=self.root / "private", operator_home=self.root / "operator")
        return launch_rootless(self.runner, **(options | overrides))

    def assert_private_home(self):
        self.launch()
        self.assertEqual(self.runner.events, [
            ("start", "start", self.binding.display, "firefox", self.root / "private")])

    def test_home_defaults_private_and_opt_in_matches_only_selected_app(self):
        self.assert_private_home()
        self.launch(persistent_home_apps=("firefox",))
        self.launch(app="other-app", persistent_home_apps=("firefox",))
        self.assertEqual([row[-1] for row in self.runner.events], [
            self.root / "private", self.root / "operator", self.root / "private"])

    def test_private_home_must_not_alias_operator_home(self):
        operator_home = self.root / "operator"
        operator_home.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(operator_home)
        with self.assertRaisesRegex(HandoffRefused, "HOLD"):
            self.launch(private_home=alias)
        self.assertEqual(self.runner.events, [])

    def test_start_desktop_refused_at_launch_and_handoff(self):
        with self.assertRaisesRegex(HandoffRefused, "DISPLAY_UNSUPPORTED"):
            self.launch(mode="start-desktop")
        self.runner.server_mode = "start-desktop"
        self.assertEqual(self.call("vp_human_take").code, "DISPLAY_UNSUPPORTED")
        self.assertEqual(self.runner.events, [])

    def test_host_coordinates_refused_before_vision_dispatch(self):
        dispatch = Mock()
        primary_code = "SNAP_CDP_UNAVAILABLE"
        fallback = VisionFallback(routes=(("cdp", primary_code),), dispatch=dispatch)
        request = dict(example("vp.channel.req"), op="act", action="click", dry_run=False)
        reply = fallback.run(request, session=example("vp.session"), observation=None,
                             point=(2, 3), primary_code=primary_code, primary_sent=False)
        self.assertEqual(reply["code"], "DISPLAY_UNSUPPORTED")
        dispatch.assert_not_called()
        for display in (":0", "xpra:0", "xpra:00"):
            with self.subTest(display=display):
                with self.assertRaisesRegex(HandoffRefused, "DISPLAY_UNSUPPORTED"):
                    self.launch(display=display)
        self.assertEqual(self.runner.events, [])

    def test_ambiguous_or_replaced_source_refuses_before_attach(self):
        other = replace(self.binding, xid=0x456, pid=456)
        self.runner.source = (self.binding, other)
        self.assertEqual(self.call("vp_human_take").code, "DISPLAY_UNSUPPORTED")
        self.runner.source = (replace(self.binding, pid=999),)
        self.assertEqual(self.call("vp_human_take").code, "identity_mismatch")
        self.assertEqual(self.runner.events, [])

    def test_uncertain_attach_can_be_verified_on_explicit_retry_without_resend(self):
        real_attach = self.runner.attach

        def sent_then_timeout(binding):
            real_attach(binding)
            raise TimeoutError

        with patch.object(self.runner, "attach", side_effect=sent_then_timeout):
            self.assertEqual(self.call("vp_human_take").code, "UNCERTAIN_OUTCOME")
            self.assertEqual(self.broker.lease(self.sid)["state"], "HUMAN")
            self.assertEqual(self.call("vp_human_take").code, "OK")
        self.assertEqual(self.runner.events, [("attach", self.binding)])

    def test_return_rejects_unverified_detach_and_keeps_agent_fenced(self):
        self.assertEqual(self.call("vp_human_take").code, "OK")
        with patch.object(self.runner, "detach", return_value=None):
            self.assertEqual(self.call("vp_human_return").code, "UNCERTAIN_OUTCOME")
        self.assertEqual(self.broker.lease(self.sid)["state"], "HUMAN")
        self.assertEqual(self.runtime.attachments(), ())

    def test_unavailable_readback_after_attach_keeps_agent_fenced(self):
        with patch.object(self.runner, "forwarded", side_effect=[(), OSError]):
            self.assertEqual(self.call("vp_human_take").code, "UNCERTAIN_OUTCOME")
        self.assertEqual(self.runner.events, [("attach", self.binding)])
        self.assertEqual(self.broker.lease(self.sid)["state"], "HUMAN")
        self.assertEqual(self.runtime.attachments(), ())

    def test_mutant_destroy_recreate_rejected_by_cycle_oracle(self):
        detach = self.runner.detach

        def recreate(binding):
            detach(binding)
            self.runner.source = (replace(binding, pid=binding.pid + 1),)

        with patch.object(self.runner, "detach", side_effect=recreate):
            with self.assertRaisesRegex(AssertionError, "identity_mismatch"):
                self.assert_cycle()
        self.assertEqual(self.runner.source[0].pid, self.binding.pid + 1)

    def test_mutant_persistent_home_by_default_rejected_by_launch_oracle(self):
        launch = self.launch
        with patch.object(self, "launch", side_effect=lambda: launch(persistent_home_apps=("firefox",))):
            with self.assertRaises(AssertionError):
                self.assert_private_home()
        self.assertEqual(self.runner.events[0][-1], self.root / "operator")

    def test_mutant_skip_fence_rejected_by_s4_presentation_gate(self):
        with patch.object(self.broker, "fence", return_value=None) as skipped_fence:
            self.assertEqual(self.call("vp_human_take").code, "identity_mismatch")
        skipped_fence.assert_called_once()
        self.assertEqual(self.runner.events, [])
        self.assertEqual(self.broker.lease(self.sid)["state"], "AGENT")

    def test_mutant_attach_outside_s4_is_observed_when_guard_is_removed(self):
        with patch.object(self.handoff.windows, "_authorize", return_value=None):
            with self.assertRaises(AssertionError):
                self.test_attach_outside_s4_refuses_even_with_broker_fence()
        self.assertEqual(self.runner.events, [("attach", self.binding)])


if __name__ == "__main__":
    unittest.main()
