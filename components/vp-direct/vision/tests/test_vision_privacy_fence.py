"""A shared host sentinel must fence S5 captures outside VisionMode.analyze."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_vision as fixtures
from frames.source import FrameUnavailable
from perception.lifecycle import LifecycleError


class VisionPrivacyFenceTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ModeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.f.bind_hidden_host()

    def provision_standalone_owner(self):
        f = self.f
        broker = fixtures.Broker(f.workspace / "s5")
        self.addCleanup(broker.close)
        broker.bind("session", f.binding, lease_state="AGENT")
        return f.frames.provision_lifecycle(
            broker, "session", f.binding, f.lifecycle.demand, f.lifecycle._agent_demand, f.stream,
            qualified=lambda _: True, runtime=lambda: fixtures.RuntimeEvidence("inc", (), 0),
            drain_agent=lambda _: True)

    def check_s5(self, *, standalone=False):
        f = self.f
        if standalone:
            # Bind P4 through the frames port without constructing a VisionMode.
            with patch.object(f, "make_mode", return_value=f.mode):
                f.provision_host_privacy()
            owner = self.provision_standalone_owner()
        else:
            f.provision_host_privacy()
            owner = f.lifecycle
        observation = owner.vp_perception_call({"op": "wake", "reason": "fixture",
                                                "session_id": "session"})["fresh_observation_id"]
        f.host_evidence = replace(f.host_evidence, revision=2, reauthenticating=True)
        with self.assertRaises(FrameUnavailable) as caught:
            f.frames._source.capture(f.capture_demand)
        self.assertEqual(caught.exception.code, "HUMAN_NEEDED")
        f.host_handoff.assert_called_once_with("login")
        self.assertEqual(owner.broker.lease("session")["state"], "AGENT")
        # The handoff port has not published a broker notice. Both a live
        # owner and a replacement must already see the durable pre-notice hold.
        with patch.object(f.stream, "read", wraps=f.stream.read) as read:
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                owner.read_frame(observation)
            read.assert_not_called()
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            f.frames.provision_lifecycle(owner.broker, "session", f.binding, owner.demand, owner._agent_demand,
                                         f.stream, qualified=self.fail, runtime=self.fail, drain_agent=self.fail)
        f.cdp.assert_not_called()
        f.atspi.assert_not_called()
        f.planner.plan_step.assert_not_called()

    def test_s5_capture_fences_mode_owner_and_replacement(self):
        self.check_s5()

    def test_s5_capture_fences_provisioned_owner_without_mode(self):
        self.check_s5(standalone=True)

    def test_mutant_s5_skips_bound_owner_before_notice(self):
        with patch.object(fixtures.PerceptionLifecycle, "hold_for_privacy", return_value=None):
            with self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
                self.check_s5()

    def test_mutant_provision_lifecycle_skips_s5_owner_binding(self):
        with patch.object(fixtures.VisionFrames, "_bind_lifecycle", return_value=False):
            with self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
                self.check_s5(standalone=True)

    def check_human_drain(self, *, external=False):
        f = self.f
        def transfer(reason):
            f.broker.human_needed(f.binding["owner"], "session", "goal", reason)
            f.broker.fence("session", f.binding["authority_epoch"], reason, {})
        f.provision_host_privacy(request_human=transfer)
        observation = f.start()
        f.mode.analyze(observation)
        f.host_evidence = replace(f.host_evidence, revision=2, login_active=True)
        def external_capture(state):
            # A directly provisioned S5 port can finish handoff before
            # VisionMode receives its first privacy refusal.
            transfer("login")
            raise FrameUnavailable("login", code="HUMAN_NEEDED")
        capture = external_capture if external else f.frames.capture
        with patch.object(f.frames, "capture", side_effect=capture):
            with self.assertRaises(fixtures.VisionError) as caught:
                f.mode.analyze(observation)
        self.assertEqual(f.broker.lease("session")["state"], "HUMAN")
        self.assertEqual(caught.exception.code, "HUMAN_NEEDED", "lost privacy verdict")
        f.provider.close.assert_called_once_with()
        f.planner.plan_step.assert_called_once()
        f.atspi.assert_called_once()
        f.cdp.assert_called_once()

    def test_human_transfer_preserves_privacy_verdict_and_drains(self):
        self.check_human_drain()

    def test_first_hold_after_s5_handoff_still_drains(self):
        self.check_human_drain(external=True)

    def test_mutant_rechecking_lost_authority_masks_privacy_verdict(self):
        hold = self.f.lifecycle.hold_for_privacy
        def recheck(*, before_notice=False):
            if not before_notice:
                self.f.lifecycle._authority()
            return hold(before_notice=before_notice)
        with patch.object(self.f.lifecycle, "hold_for_privacy", side_effect=recheck):
            with self.assertRaisesRegex(AssertionError, "lost privacy verdict"):
                self.check_human_drain()

    def check_owner_binding(self, field):
        f = self.f
        f.provision_host_privacy()
        alternate = Mock(binding=dict(f.binding, **{field: 99 if field == "authority_epoch" else "other"}))
        fixtures.VisionMode(alternate, f.frames, f.adapters, session_origin_ns=0)
        f.host_evidence = replace(f.host_evidence, revision=2, login_active=True)
        with self.assertRaises(FrameUnavailable) as caught:
            f.frames._source.capture(f.capture_demand)
        self.assertEqual(caught.exception.code, "HUMAN_NEEDED")
        alternate.hold_for_privacy.assert_not_called()
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            f.lifecycle.wake_for_verified()

    def test_direct_s5_uses_only_the_bound_owner(self):
        for field in ("owner", "surface_id", "incarnation", "authority_epoch"):
            with self.subTest(field=field):
                self.check_owner_binding(field)

    def test_mutant_unchecked_owner_registration_misdirects_s5_hold(self):
        def bind_any(frames, owner):
            frames._privacy_owner = owner
        with patch.object(fixtures.VisionFrames, "_bind_lifecycle", bind_any):
            with self.assertRaisesRegex(AssertionError, "hold_for_privacy.*not have been called"):
                self.check_owner_binding("surface_id")

    def check_s5_before_first_mode_capture(self, *, shared=False, opaque=False):
        f = self.f

        class Interrupted(BaseException):
            pass

        with patch.object(f, "make_mode", return_value=f.mode):
            f.provision_host_privacy(record_gap=Mock(side_effect=Interrupted))
        capture = f.frames.capture
        if opaque:
            f.lifecycle = self.provision_standalone_owner()
            f.broker = f.lifecycle.broker
            capture = Mock(side_effect=capture)
        port = SimpleNamespace(current=f.frames.current, capture=capture)
        f.mode = fixtures.VisionMode(f.lifecycle, port, f.adapters, session_origin_ns=0)
        if shared:
            alternate = Mock(binding=f.binding)
            fixtures.VisionMode(alternate, port, f.adapters, session_origin_ns=0)
        observation = f.start()
        f.host_evidence = replace(f.host_evidence, revision=2, login_active=True)

        with self.assertRaises(Interrupted):
            if shared:
                f.mode.analyze(observation)
            else:
                f.frames._source.capture(f.capture_demand)
        if shared:
            alternate.hold_for_privacy.assert_not_called()
        f.host_handoff.assert_not_called()
        self.assertIsNone(f.lifecycle._last_login_notice(f.broker))
        with patch.object(f.stream, "read", wraps=f.stream.read) as read:
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                f.lifecycle.read_frame(observation)
            read.assert_not_called()
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            f.frames.provision_lifecycle(f.broker, "session", f.binding, f.lifecycle.demand,
                                         f.lifecycle._agent_demand, f.stream,
                                         qualified=self.fail, runtime=self.fail, drain_agent=self.fail)
        f.cdp.assert_not_called()
        f.atspi.assert_not_called()
        f.provider_factory.assert_not_called()
        f.planner.plan_step.assert_not_called()

    def test_direct_s5_fences_delegated_port_before_first_mode_capture(self):
        self.check_s5_before_first_mode_capture()

    def test_direct_s5_fences_opaque_port_with_source_provisioned_owner(self):
        self.check_s5_before_first_mode_capture(opaque=True)

    def test_mutant_delegated_port_skips_eager_owner_registration(self):
        with patch.object(fixtures.VisionFrames, "_bind_lifecycle", return_value=False):
            with self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
                self.check_s5_before_first_mode_capture()

    def test_shared_delegated_capture_fences_the_requesting_owner(self):
        self.check_s5_before_first_mode_capture(shared=True)

    def check_delegated_capture(self):
        f = self.f
        class Interrupted(BaseException):
            pass

        def interrupt(active, when):
            raise Interrupted

        with patch.object(f, "make_mode", return_value=f.mode):
            f.provision_host_privacy(record_gap=interrupt)
        port = SimpleNamespace(current=f.frames.current, capture=lambda state: f.frames.capture(state))
        f.mode = fixtures.VisionMode(f.lifecycle, port, f.adapters, session_origin_ns=0)
        obs = f.start()
        self.assertEqual(f.mode.analyze(obs)["plan"], ("fixture-candidate",))
        f.host_evidence = replace(f.host_evidence, revision=2, login_active=True)
        with self.assertRaises(Interrupted):
            f.mode.analyze(obs)
        f.host_handoff.assert_not_called()
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            f.lifecycle.read_frame(obs)

    def test_existing_frame_port_can_supply_current_and_capture(self):
        self.check_delegated_capture()

    def test_mutant_unbound_delegated_capture_loses_pre_notice_hold(self):
        with patch.object(fixtures.VisionFrames, "_bind_lifecycle", return_value=None):
            with self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
                self.check_delegated_capture()

    def check_custom_port_privacy_refusal(self):
        f = self.f
        f.provision_host_privacy()
        capture = Mock(side_effect=f.frames.capture)
        port = SimpleNamespace(current=f.frames.current, capture=capture)
        f.mode = fixtures.VisionMode(f.lifecycle, port, f.adapters, session_origin_ns=0)
        observation = f.start()
        self.assertEqual(f.mode.analyze(observation)["plan"], ("fixture-candidate",))
        capture.side_effect = FrameUnavailable("login", code="HUMAN_NEEDED")

        f.expect_refusal("HUMAN_NEEDED", observation)
        f.host_handoff.assert_not_called()
        self.assertIsNone(f.lifecycle._last_login_notice(f.broker))
        self.assertEqual(f.broker.lease("session")["state"], "AGENT")
        f.provider.close.assert_called_once_with()
        f.planner.plan_step.assert_called_once()
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            f.lifecycle.read_frame(observation)

    def test_custom_port_human_needed_fences_lifecycle_without_broker_notice(self):
        self.check_custom_port_privacy_refusal()

    def test_mutant_custom_port_drops_fallback_privacy_hold(self):
        with patch.object(self.f.lifecycle, "hold_for_privacy", return_value=None):
            with self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
                self.check_custom_port_privacy_refusal()

    def check_nested_s5(self, *, use_outer_owner=False):
        outer = self.f
        outer.provision_host_privacy()
        inner = fixtures.ModeTests()
        self.addCleanup(inner.doCleanups)
        inner.setUp()
        inner.bind_hidden_host()
        inner.provision_host_privacy()
        inner.host_evidence = replace(inner.host_evidence, revision=2, login_active=True)
        capture = outer.frames.capture
        def nested(state):
            with self.assertRaises(FrameUnavailable) as refused:
                inner.frames._source.capture(inner.capture_demand)
            self.assertEqual(refused.exception.code, "HUMAN_NEEDED")
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                inner.lifecycle.wake_for_verified()
            return capture(state)
        owner = outer.lifecycle if use_outer_owner else inner.lifecycle
        with patch.object(inner.frames, "_privacy_owner", owner), patch.object(outer.frames, "capture", side_effect=nested):
            self.assertEqual(outer.mode.analyze(outer.start())["plan"], ("fixture-candidate",))
        inner.cdp.assert_not_called()

    def test_nested_s5_hold_stays_with_its_source(self):
        self.check_nested_s5()

    def test_mutant_nested_s5_uses_outer_capture_owner(self):
        with self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
            self.check_nested_s5(use_outer_owner=True)
