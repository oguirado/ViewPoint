"""Exercise privacy admission with real session, timeline and stream owners."""
from contextlib import ExitStack
from time import monotonic_ns
import unittest
from unittest.mock import Mock, patch

import test_lifecycle as fixtures
from human import FreshObservation
from perception.demand import DemandCoordinator
from perception.lifecycle import LifecycleError
from recorder.timeline import TimelinePosition, TimelineRecorder


class PrivacyFenceTests(unittest.TestCase):
    def fixture(self):
        f = fixtures.LifecycleTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        return f

    def gap_handback(self, *, captured_after=False):
        f = self.fixture()
        recorder = TimelineRecorder(surface_id=f.p.surface, capsule_id="capsule",
                                    incarnation=f.binding["incarnation"], register=f.src.register,
                                    verify_ram_frame=self.fail)
        take = f.b.fence("session", f.binding["authority_epoch"], "login", {})
        lease = f.b.prepare_return("session", take["lease"]["epoch_after"])
        f.binding = f.b.binding(f.p.surface)
        with recorder.recording(f.recording):
            recorder.set_gap(TimelinePosition(monotonic_ns(), "capsule", f.binding["incarnation"]),
                             "privacy", active=True)
            captured = monotonic_ns()
            recorder.set_gap(TimelinePosition(monotonic_ns(), "capsule", f.binding["incarnation"]),
                             "privacy", active=False)
        fresh = FreshObservation("session", f.p.surface, f.binding["incarnation"], lease["epoch_after"],
                                 "returned", monotonic_ns() if captured_after else captured, 1)
        f.b.finish_return(f.b.record_return(lease), fresh)
        return f, recorder.snapshot()

    def check_gap_handback(self, *, omit_end=False):
        f, timeline = self.gap_handback()
        if omit_end:
            timeline["events"].pop()
        clear = Mock(return_value=True)
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            f.provision(privacy_timeline=lambda: timeline, confirm_privacy_clear=clear)
        clear.assert_not_called()

    def test_closed_gap_rejects_observation_captured_inside(self):
        self.check_gap_handback()

    def test_closed_gap_fences_replayed_active_goal_before_readback(self):
        f = self.fixture()
        f.start_recoverable_active_goal()
        recorder = TimelineRecorder(surface_id=f.p.surface, capsule_id="capsule",
                                    incarnation=f.binding["incarnation"], register=f.src.register,
                                    verify_ram_frame=self.fail)
        with recorder.recording(f.recording):
            for active in (True, False):
                recorder.set_gap(TimelinePosition(monotonic_ns(), "capsule", f.binding["incarnation"]),
                                 "privacy", active=active)
        f.assert_recovery_refuses_privacy_hold(privacy_timeline=recorder.snapshot, confirm_privacy_clear=self.fail)

    def test_mutant_missing_end_boundary_accepts_inside_observation(self):
        with self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
            self.check_gap_handback(omit_end=True)

    def test_post_gap_handback_requires_clear_then_reaches_ready(self):
        f, timeline = self.gap_handback(captured_after=True)
        f.replace_stream()
        clear = Mock(side_effect=[False, True])
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            f.provision(privacy_timeline=lambda: timeline, confirm_privacy_clear=clear)
        f.p = f.provision(privacy_timeline=lambda: timeline, confirm_privacy_clear=clear)
        observation = f.call("wake")["fresh_observation_id"]
        self.assertEqual(f.goal(observation).replay()["phase"], "READY")
        self.assertEqual(f.p.read_frame(observation), b"red" * 32)
        self.assertEqual(clear.call_count, 2)

    def raise_sibling_hold(self, f):
        binding = dict(f.binding, surface_id="sibling", endpoint="sibling-endpoint")
        f.b.bind("session", binding)
        demand = dict(f.demand, surface_id="sibling")
        source = DemandCoordinator("sibling", interests=["VERIFIED"], authorize=self.fail, drain=self.fail)
        with patch.object(f, "binding", binding), patch.object(f, "stream", None):
            f.replace_stream()
            sibling = fixtures.PerceptionLifecycle(
                f.b, "session", binding, source, demand, f.stream,
                qualified=self.fail, runtime=lambda: f.sample, drain_agent=self.fail)
        sibling.hold_for_privacy()

    def check_live_operation(self, operation, source, *, blind=False):
        f = self.fixture()
        obs = f.call("wake")["fresh_observation_id"]
        with patch.object(f.p, "bind_goal"):
            goal = f.goal(obs)
        if operation == "wake":
            f.call("off")
        if source == "sibling":
            self.raise_sibling_hold(f)
        else:
            f.b.human_needed(f.owner, "session", "g", "login")
        self.assertEqual(f.b.lease("session")["state"], "AGENT")
        perform = Mock()
        with ExitStack() as ports:
            if blind:
                ports.enter_context(patch.object(f.p, "_privacy_boundary", return_value=(None, None)))
            spies = [ports.enter_context(patch.object(obj, name, wraps=getattr(obj, name)))
                     for obj, name in ((f.stream, "read"), (f.stream, "wake"), (f.src, "claim"), (goal, "replay"))]
            calls = {"wake": lambda: f.call("wake"), "verified": f.p.wake_for_verified,
                     "goal": lambda: f.p.bind_goal(goal),
                     "frame": lambda: f.p.read_frame(obs), "agent": lambda: f.p.run_agent("decision", obs, perform)}
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                calls[operation]()
            for spy in [*spies, perform]:
                spy.assert_not_called()

    def test_all_live_entries_observe_session_hold_before_readback(self):
        for operation in ("wake", "verified", "goal", "frame", "agent"):
            for source in ("sibling", "notice"):
                with self.subTest(operation=operation, source=source):
                    self.check_live_operation(operation, source)

    def test_mutant_live_entries_ignore_durable_boundary(self):
        for operation in ("wake", "verified", "goal", "frame", "agent"):
            with self.subTest(operation=operation):
                source = "sibling" if operation == "wake" else "notice"
                with self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
                    self.check_live_operation(operation, source, blind=True)

    def test_unrelated_login_notice_allows_live_read(self):
        f = self.fixture()
        obs = f.call("wake")["fresh_observation_id"]
        f.b._write("HUMAN_NEEDED", session_id="elsewhere", goal_id="g", reason="login", raised_by="other")
        self.assertEqual(f.p.read_frame(obs), b"red" * 32)

    def check_replacement_history(self, *, reuse_offset=False):
        f = self.fixture()
        goal = f.goal(f.call("wake")["fresh_observation_id"])
        f.call("sleep")
        self.assertIsNone(f.p.wake_for_verified())
        f.b.close()
        replacement = fixtures.Broker(f.root / "separate-history")
        self.addCleanup(replacement.close)
        replacement.bind("session", f.binding, lease_state="AGENT")
        replacement.human_needed(f.owner, "session", "g", "login")
        if reuse_offset:
            _, offset, notice = f.p._login_notice_cursor
            f.p._login_notice_cursor = (replacement._journal, offset, notice)
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            f.p.reconnect_broker(replacement, goal)

    def test_replacement_reads_login_before_old_journal_offset(self):
        self.check_replacement_history()

    def test_mutant_carries_old_offset_to_different_journal(self):
        with self.assertRaisesRegex(AssertionError, 'does not match "identity_mismatch"'):
            self.check_replacement_history(reuse_offset=True)

    def check_goal_replay_boundary(self, *, blind=False):
        f = self.fixture()
        obs = f.call("wake")["fresh_observation_id"]
        with patch.object(f.p, "bind_goal"):
            goal = f.goal(obs)
        ledger = goal.replay()
        def replay():
            f.b.human_needed(f.owner, "session", "g", "login")
            return ledger
        with ExitStack() as ports:
            ports.enter_context(patch.object(goal, "replay", side_effect=replay))
            if blind:
                ports.enter_context(patch.object(f.p, "_privacy_boundary", return_value=(None, None)))
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                f.p.bind_goal(goal)
        self.assertIsNone(f.p._goal)

    def test_goal_replay_cannot_cross_new_login_notice(self):
        self.check_goal_replay_boundary()

    def test_mutant_goal_attachment_trusts_pre_replay_fence(self):
        with self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
            self.check_goal_replay_boundary(blind=True)

    def check_wake_boundary(self, when, *, blind=False):
        f = self.fixture()
        qualify = f.p._qualified
        wake = f.stream.wake
        read_frame = f.stream.read
        def notice():
            f.b.human_needed(f.owner, "session", "g", "login")
        def qualify_then_hold(binding):
            result = qualify(binding)
            if when == "before-start":
                notice()
            return result
        def wake_then_hold():
            obs = wake()
            if when == "before-read":
                notice()
            return obs
        def read_then_hold(*args):
            pixels = read_frame(*args)
            if when == "during-read":
                notice()
            return pixels
        with ExitStack() as ports:
            ports.enter_context(patch.object(f.p, "_qualified", side_effect=qualify_then_hold))
            start = ports.enter_context(patch.object(f.stream, "wake", side_effect=wake_then_hold))
            read = ports.enter_context(patch.object(f.stream, "read", side_effect=read_then_hold))
            if blind:
                ports.enter_context(patch.object(f.p, "_privacy_boundary", return_value=(None, None)))
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                f.call("wake")
            self.assertEqual(start.call_count, 0 if when == "before-start" else 1)
            self.assertEqual(read.call_count, 1 if when == "during-read" else 0)
        self.assertEqual(f.state(), "OFF")

    def test_wake_rechecks_before_start_and_frame_read(self):
        for when in ("before-start", "before-read", "during-read"):
            with self.subTest(when=when):
                self.check_wake_boundary(when)

    def test_mutant_wake_worker_trusts_initial_admission(self):
        for when in ("before-start", "before-read", "during-read"):
            with self.subTest(when=when), self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
                self.check_wake_boundary(when, blind=True)

    def test_returned_stream_rejects_prior_ring_slot_before_readback(self):
        f = self.fixture()
        f.call("wake")
        f.p.hold_for_privacy()
        f.stream.close()
        take = f.b.fence("session", f.binding["authority_epoch"], "login", {})
        lease = f.b.prepare_return("session", take["lease"]["epoch_after"])
        f.binding = f.b.binding(f.p.surface)
        fresh = FreshObservation("session", f.p.surface, f.binding["incarnation"],
                                 lease["epoch_after"], "channel-return", monotonic_ns(), 1)
        f.b.finish_return(f.b.record_return(lease), fresh)
        f.replace_stream()
        older = f.stream.wake()
        current = f.stream.observe(older.token)
        self.assertNotEqual(older.observation_id, current.observation_id)
        f.src = f.durable_source("returned.sqlite")
        with patch.object(f.stream, "read", side_effect=self.fail) as read:
            with self.assertRaisesRegex(LifecycleError, "^STALE_FRAME$"):
                f.provision(confirm_privacy_clear=lambda _: True, recover_observation=lambda: older)
        read.assert_not_called()

    def check_retired_owner(self, *, stale_take=False):
        f = self.fixture()
        take = f.b.fence("session", f.binding["authority_epoch"], "operator", {})
        lease = f.b.prepare_return("session", take["lease"]["epoch_after"])
        fresh = FreshObservation("session", f.p.surface, f.binding["incarnation"],
                                 lease["epoch_after"], "returned", monotonic_ns(), 1)
        f.b.finish_return(f.b.record_return(lease), fresh)
        f.b.fence("session", lease["epoch_after"], "operator", {})
        with ExitStack() as ports:
            if stale_take:
                ports.enter_context(patch.object(f.b, "history", return_value=(take, None)))
            write = ports.enter_context(patch.object(f.p, "_login_fence", wraps=f.p._login_fence))
            f.p.hold_for_privacy()
            write.assert_not_called()
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            f.p.wake_for_verified()

    def test_retired_owner_cannot_write_into_a_later_handoff(self):
        self.check_retired_owner()

    def test_mutant_using_prior_take_writes_into_later_handoff(self):
        with self.assertRaisesRegex(AssertionError, "_login_fence.*not have been called"):
            self.check_retired_owner(stale_take=True)
