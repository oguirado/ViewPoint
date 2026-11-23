from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
import inspect
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
from threading import Event
from time import monotonic_ns
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

DIRECT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(DIRECT), str(DIRECT.parent / "wender-agent")]
from bus.broker import Broker, BrokerError, call
from bus.journal import Journal
from channel.cdp import CdpBackend, Refused
from channel.cdp.tests.test_cdp import FakePort, reply_visible
from human import FocusWitness, FreshObservation, HandoffBroker, HandoffRefused, HandoffWindows, HumanHandoff
from human.channels import SessionRuntime, _request
from recorder.timeline import TimelinePosition, TimelineRecorder
from schemas.validation import validate_contract
from session.registry import SessionRegistry


def example(name):
    return json.loads((DIRECT / "schemas/tests/examples" / f"{name}_v1.valid.json").read_bytes())


class Desktop:
    """Small X server/window-manager stand-in. Logs actual argv at the boundary."""
    def __init__(self):
        self.active = 0x456
        self.workspaces = {0x456: 0, 0x123: 1}
        self.witnesses = {0x456: "operator-incarnation", 0x123: "fixture-witness"}
        self.moves = []
        self.on_move = lambda workspace: None

    def run(self, args, **kwargs):
        if args[:2] == ("xprop", "-root"):
            output = (f"_NET_CURRENT_DESKTOP(CARDINAL) = 0\n_NET_NUMBER_OF_DESKTOPS(CARDINAL) = 4\n"
                      f"_NET_ACTIVE_WINDOW(WINDOW): window id # {hex(self.active)}")
        elif args == ("/fixture/srf-host-window", "list"):
            output = json.dumps([dict(xid=x, witness=w) for x, w in self.witnesses.items()])
        elif args[:2] == ("xprop", "-id"):
            xid, prop = int(args[2], 16), args[3]
            value = self.workspaces[xid] if prop == "_NET_WM_DESKTOP" else {0x456: 444, 0x123: 123}[xid]
            output = f"{prop}(CARDINAL) = {value}"
        elif args[:3] == ("wmctrl", "-i", "-r"):
            xid, workspace = int(args[3], 16), int(args[5])
            self.on_move(workspace)
            self.moves.append(("move", xid, workspace))
            self.workspaces[xid] = workspace
            if xid == self.active:
                self.active = 0x999  # The WM may select a different operator window.
            output = ""
        elif args[:3] == ("wmctrl", "-i", "-a"):
            self.active = int(args[3], 16)
            self.moves.append(("restore", self.active))
            output = ""
        else:
            raise AssertionError(args)
        return SimpleNamespace(stdout=output)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.registry = SessionRegistry({}, None, data_home=self.root / "data")
        self.session = example("vp.session")
        self.registry.register_attached(self.session)
        self.sid = self.session["session_id"]
        self.owner = self.session["lease"]["holder"]
        self.inc = self.session["incarnation"]
        self.surface = "surface"
        self.broker = HandoffBroker(self.root / "rt")
        self.addCleanup(lambda: self.broker.close())
        self.broker.bind(self.sid, dict(example("vp.surface_binding"), owner=self.owner,
                                        incarnation=self.inc), lease_state="AGENT")
        self.journal = Journal(self.root)
        self.desktop = Desktop()
        self.wl0 = json.loads((DIRECT / "config/wl0.json").read_bytes())
        windows = HandoffWindows("/fixture", self.registry.get_session, self.wl0,
                                 run=self.desktop.run, pointer=lambda: (20, 30))
        self.ports = []
        self.opened = []
        self.prior = self.open_cdp("initial", self.session)
        self.prior.attach_target(_request("attach", self.session))
        self.old = self.prior.observe(_request("observe", self.session))
        self.aux = Mock(spec=["detach", "close"])
        self.proof = None
        self.order = []
        self.runtime = SessionRuntime(self.sid, executors={"cdp": self.prior, "atspi": self.aux},
                                     open_executor=self.open_cdp, qualify=self.qualify, retain=self.retain,
                                     reconcile=self.reconcile, observation_id=lambda: self.old.id,
                                     generation=lambda: 1, attachment_readback=lambda: tuple(
                                         (["cdp"] if any(not p.closed for p in self.ports) else [])
                                         + (["atspi"] if not self.aux.close.called else [])))
        self.rec = TimelineRecorder(surface_id=self.surface, capsule_id=self.session["capsule_id"],
                                    incarnation=self.inc, register=lambda _: nullcontext(),
                                    verify_ram_frame=lambda *_: True)
        self.enterContext(self.rec.recording(example("vp.demand")))
        self.handoff = HumanHandoff(broker=self.broker, registry=self.registry, session_id=self.sid,
                                    surface_id=self.surface, windows=windows, runtime=self.runtime,
                                    lineage=self.journal, goal_id="fixture-goal", recorder=self.rec)

    def open_cdp(self, mode, session):
        if mode != "initial":
            self.order.append(("connect", mode, session["lease"]["authority_epoch"]))
        port = FakePort()
        self.ports.append(port)
        read_session = self.handoff.read_executor_session if mode != "initial" else self.registry.get_session
        channel = CdpBackend(self.sid, read_session, connect=lambda _: port,
                             effect_guard=lambda _: nullcontext())
        self.opened.append(channel)
        return channel

    def retain(self, session, binding):
        self.order.append(("retain",))
        self.assertEqual(self.runtime.attachments(), ())
        return dict(schema="vp.perception_state/v1", session_id=self.sid, surface_id=self.surface,
                    incarnation=self.inc, authority_epoch=binding["authority_epoch"], state="ASLEEP",
                    stream_generation=1, obligations=["recording"], since_mono_ns=monotonic_ns())

    def qualify(self, session, binding):
        self.order.append(("reauthorize",))
        return session["lease"]["authority_epoch"] == binding["authority_epoch"]

    def reconcile(self, lock):
        self.order.append(("reconcile", lock["status"], self.broker.lease(self.sid)["state"]))
        return self.proof

    def request(self, op, caller="operator", **changes):
        args = {"session_id": self.sid}
        if op == "vp_human_take":
            args["reason"] = "login"
        ctx = dict(caller=caller, agent_instance=self.owner if caller == "agent" else "operator",
                   session_id=self.sid, incarnation=self.inc,
                   authority_epoch=self.broker.lease(self.sid)["authority_epoch"])
        return dict(schema="vp.control.req/v1", request_id=uuid4().hex, op=op, context=ctx,
                    args=args, dry_run=False) | changes

    def take(self):
        return self.handoff.vp_human_take(self.request("vp_human_take"))

    def back(self):
        return self.handoff.vp_human_return(self.request("vp_human_return"))

    def issue(self):
        lock = self.broker.try_acquire(self.owner, self.surface, self.inc, 1, "sent-once")
        self.broker.dispatch(self.owner, lock)
        return lock

    def test_take_fences_reconciles_detaches_retains_then_summons(self):
        self.issue()
        def before_move(workspace):
            self.assertEqual(self.broker.lease(self.sid)["state"], "HUMAN")
            self.assertEqual(self.order, [("reconcile", "UNKNOWN", "HUMAN"), ("retain",)])
            self.assertTrue(self.ports[0].closed)
            self.aux.close.assert_called_once()
            self.assertEqual(self.broker.perception_state(self.surface)["obligations"], ["recording"])
        self.desktop.on_move = before_move
        result = self.take()
        self.assertTrue(result.ok)
        lease = result.result["lease"]
        validate_contract("vp.human_lease/v1", lease)
        self.assertEqual(lease["outstanding"], [{"request_id": "sent-once", "disposition": "UNCERTAIN_OUTCOME"}])
        self.assertEqual(self.broker.history(self.sid)[0]["handoff"]["focus"]["xid"], 0x456)
        self.assertEqual(self.runtime.attachments(), ())

    def test_return_restores_focus_bumps_epoch_reconnects_and_requires_new_readback(self):
        self.assertTrue(self.take().ok)
        self.desktop.active = 0x123
        result = self.back()
        self.assertTrue(result.ok)
        self.assertEqual(self.desktop.moves, [("move", 0x123, 0), ("move", 0x123, 1), ("restore", 0x456)])
        self.assertEqual(self.order, [("retain",), ("connect", "reconnect", 3), ("reauthorize",)])
        self.assertEqual(self.desktop.active, 0x456)
        self.assertEqual(self.broker.lease(self.sid), dict(state="AGENT", holder=self.owner, authority_epoch=3))
        self.assertNotEqual(result.result["fresh_observation_id"], self.old.id)
        events = self.journal.verify()
        self.assertEqual([(e["payload"]["state"], e["lamport"]) for e in events], [("HUMAN", 1), ("AGENT", 2)])

    def test_pre_take_observation_refused_new_observation_allows_one_verified_act(self):
        self.assertTrue(self.take().ok)
        result = self.back()
        self.assertTrue(result.ok)
        session, _ = self.handoff._session()
        channel = self.opened[-1]
        action = dict(_request("act", session), action="click", observation_id=self.old.id)
        action["target"]["ax_stable_sha256"] = self.old.ax_stable_sha256
        node_id = next(n["id"] for n in self.old.nodes if n["role"] == "button")
        with self.assertRaises(Refused):
            channel.act(action, node_id=node_id, verify=reply_visible)
        self.assertFalse(self.ports[-1].clicked)
        action["observation_id"] = result.result["fresh_observation_id"]
        self.assertTrue(channel.act(action, node_id=node_id, verify=reply_visible)["ok"])
        self.assertEqual(sum(m == "Runtime.callFunctionOn" for m, _, _ in self.ports[-1].calls), 1)

    def test_uncertain_send_blocks_return_and_cannot_be_retried_after_reconciliation(self):
        self.issue()
        self.assertTrue(self.take().ok)
        self.assertEqual(self.back().code, "UNCERTAIN_OUTCOME")
        self.assertEqual(len(self.ports), 1)
        self.proof = ("NO_EFFECT", "a" * 64)
        self.assertTrue(self.back().ok)
        with self.assertRaisesRegex(BrokerError, "DUPLICATE_EVENT"):
            self.broker.try_acquire(self.owner, self.surface, self.inc, 3, "sent-once")
        rows = [json.loads(l) for l in (self.root / "rt/events.jsonl").read_bytes().splitlines()]
        self.assertEqual([r["event_id"] for r in rows if r["rec"] == "turn"], ["sent-once"])

    def test_agent_request_emits_minimal_human_needed_and_has_no_return_authority(self):
        request = self.request("vp_human_take", "agent")
        self.assertEqual(self.handoff.execute(request).code, "HUMAN_NEEDED")
        event = json.loads((self.root / "rt/events.jsonl").read_bytes().splitlines()[-1])
        self.assertEqual(set(event) - {"v", "seq", "ts_ms", "by", "rec"},
                         {"session_id", "goal_id", "raised_by", "reason"})
        self.assertEqual((event["rec"], event["goal_id"], event["raised_by"]),
                         ("HUMAN_NEEDED", "fixture-goal", self.owner))
        self.assertEqual(self.desktop.moves, [])
        self.assertTrue(self.take().ok)
        for op in ("vp_human_return", "vp_human_take"):
            self.assertEqual(self.handoff.execute(self.request(op, "agent")).code, "NO_LEASE")

    def test_elapsed_time_never_returns_authority(self):
        self.assertTrue(self.take().ok)
        with patch.object(self.handoff, "clock", return_value=10**20):
            self.assertEqual(self.handoff.execute(self.request("vp_human_return", "agent")).code, "NO_LEASE")
        self.assertEqual(self.broker.lease(self.sid)["state"], "HUMAN")
        self.assertEqual(self.runtime.attachments(), ())

    def test_real_recorder_gap_covers_human_interval(self):
        self.assertTrue(self.take().ok)
        pos = TimelinePosition(monotonic_ns(), self.session["capsule_id"], self.inc)
        self.rec.add_cdp(pos, text="synthetic-secret")
        self.assertTrue(self.back().ok)
        self.assertEqual([e["op"] for e in self.rec.snapshot()["events"]], ["human_gap_start", "human_gap_end"])

    def test_agent_port_refused_at_epoch_bump_until_readback(self):
        self.assertTrue(self.take().ok)
        entered, finish = Event(), Event()
        observe = self.runtime.observe
        def paused(*args):
            entered.set()
            if not finish.wait(5):
                raise RuntimeError("test barrier timed out")
            return observe(*args)
        with self.broker.agent_port(self.owner) as agent, patch.object(self.runtime, "observe", paused):
            with ThreadPoolExecutor(1) as pool:
                result = pool.submit(self.back)
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertIsNone(self.broker.lease(self.sid)["holder"])
                    self.assertIsNone(self.broker.history(self.sid)[1])
                    with self.assertRaisesRegex(BrokerError, "NO_LEASE"):
                        call(agent, "try_acquire", target=self.surface, incarnation=self.inc,
                             authority_epoch=3, request_id="next")
                finally:
                    finish.set()
                self.assertTrue(result.result(timeout=5).ok)
            lock = call(agent, "try_acquire", target=self.surface, incarnation=self.inc,
                        authority_epoch=3, request_id="next")
            call(agent, "release", lock_id=lock)

    def test_wrong_identity_stale_epoch_and_dry_run_do_not_move(self):
        for k, v in (("authority_epoch", 9), ("incarnation", "other"), ("session_id", "other")):
            req = self.request("vp_human_take")
            req["context"][k] = v
            self.assertEqual(self.handoff.execute(req).code, "NO_LEASE")
        req = self.request("vp_human_take", dry_run=True)
        self.assertTrue(self.handoff.handlers()["vp_human_take"].preview(req).ok)
        self.assertEqual(self.desktop.moves, [])
        self.assertEqual(self.journal.verify(), [])
        self.assertFalse(self.ports[0].closed)

    def test_failed_authorization_detaches_and_restart_requires_explicit_recovery(self):
        self.assertTrue(self.take().ok)
        with patch.object(self.runtime, "reauthorize", return_value=False):
            self.assertEqual(self.back().code, "NO_LEASE")
        self.assertEqual(self.runtime.attachments(), ())
        self.broker.close()
        self.broker = self.handoff.broker = HandoffBroker(self.root / "rt")
        with self.assertRaisesRegex(BrokerError, "NO_LEASE"):
            self.broker.try_acquire(self.owner, self.surface, self.inc, 3, "next")
        self.assertTrue(self.back().ok)
        self.assertEqual(self.broker.lease(self.sid)["authority_epoch"], 3)
        self.assertEqual(len(self.journal.verify()), 2)

    def test_plain_broker_replays_s4_preparation_without_granting_input(self):
        self.assertTrue(self.take().ok)
        with patch.object(self.runtime, "reauthorize", return_value=False):
            self.assertEqual(self.back().code, "NO_LEASE")
        lease = self.broker.lease(self.sid)
        history = self.broker.history(self.sid)
        log = self.root / "rt/events.jsonl"
        existing = log.read_bytes()
        self.broker.close()
        self.broker = self.handoff.broker = Broker(self.root / "rt")
        self.assertEqual(log.read_bytes(), existing)
        self.assertEqual(self.broker.history(self.sid), history)
        self.assertEqual(self.broker.lease(self.sid), lease)
        self.assertIsNone(lease["holder"])
        with self.broker.agent_port(self.owner) as agent:
            with self.assertRaisesRegex(BrokerError, "^NO_LEASE$"):
                call(agent, "try_acquire", target=self.surface, incarnation=self.inc,
                     authority_epoch=lease["authority_epoch"], request_id="during-recovery")
        self.assertTrue(self.back().ok)
        self.assertEqual(self.broker.lease(self.sid)["authority_epoch"], lease["authority_epoch"])
        self.assertEqual(sum(json.loads(line).get("op") == "prepare_human_return"
                             for line in log.read_bytes().splitlines()), 1)

    def test_human_dispatch_refused_and_inflight_disposition_preserved(self):
        lock = self.issue()
        self.assertTrue(self.take().ok)
        with self.broker.agent_port(self.owner) as agent:
            with self.assertRaisesRegex(BrokerError, "^NO_LEASE$"):
                call(agent, "dispatch", lock_id=lock)
            with self.assertRaisesRegex(BrokerError, "^NO_LEASE$"):
                call(agent, "release", lock_id=lock)
        self.assertEqual(self.broker.history(self.sid)[0]["lease"]["outstanding"],
                         [{"request_id": "sent-once", "disposition": "UNCERTAIN_OUTCOME"}])
        self.assertEqual(self.back().code, "UNCERTAIN_OUTCOME")
        self.assertEqual(self.broker.lock_status(self.surface)["status"], "UNKNOWN")
        self.assertEqual(len([event for event in self.journal.verify()
                              if event["payload"]["state"] == "AGENT"]), 0)

    def test_mutant_agent_release_during_human_fails_exact_refusal(self):
        mutant = mutate(Broker._held, 'if lease["state"] != "AGENT" or lease["holder"] != agent_id:',
                        'if False:')
        with patch.object(Broker, "_held", mutant), self.assertRaisesRegex(AssertionError, "UNCERTAIN_OUTCOME"):
            self.test_human_dispatch_refused_and_inflight_disposition_preserved()

    def test_mutant_lost_outstanding_act_fails_disposition_oracle(self):
        mutant = mutate(Broker._change_lease, "for l in locks]}", "for l in []]}")
        with patch.object(Broker, "_change_lease", mutant), self.assertRaisesRegex(AssertionError, "sent-once"):
            self.test_human_dispatch_refused_and_inflight_disposition_preserved()

    def reopen_after_return(self):
        self.assertTrue(self.take().ok)
        self.assertTrue(self.back().ok)
        self.broker.close()
        self.broker = self.handoff.broker = Broker(self.root / "rt")
        return self.broker.lease(self.sid)["authority_epoch"]

    def assert_input_fenced(self, epoch):
        with self.broker.agent_port(self.owner) as agent:
            with self.assertRaisesRegex(BrokerError, "^NO_LEASE$"):
                call(agent, "try_acquire", target=self.surface, incarnation=self.inc,
                     authority_epoch=epoch, request_id="before-readback")
        with self.assertRaisesRegex(BrokerError, "^NO_LEASE$"):
            self.broker.returned_observation(self.sid)

    def test_canonical_restart_after_recorded_return_requires_fresh_readback(self):
        epoch = self.reopen_after_return()
        self.assert_input_fenced(epoch)
        self.assertTrue(self.back().ok)
        self.assertEqual(self.broker.lease(self.sid)["authority_epoch"], epoch)
        self.assertEqual(len(self.journal.verify()), 2)

    def test_mutant_replayed_return_grants_agent_before_readback(self):
        epoch = self.reopen_after_return()
        mutant = mutate(Broker._pending_return, "session not in self._ready",
                        "session not in self._returns")
        with patch.object(Broker, "_pending_return", mutant):
            with self.assertRaisesRegex(AssertionError, "BrokerError not raised"):
                self.assert_input_fenced(epoch)
        self.assertEqual(self.broker.lock_status(self.surface)["request_id"], "before-readback")

    def test_foreign_window_reuse_refuses_restore(self):
        self.assertTrue(self.take().ok)
        self.desktop.witnesses[0x456] = "recycled"
        self.assertEqual(self.back().code, "host_window_unavailable")
        self.assertEqual(self.broker.lease(self.sid)["state"], "HUMAN")
        self.assertFalse(any(m[0] == "restore" for m in self.desktop.moves))

    def test_focus_change_during_reconnect_refuses_return(self):
        self.assertTrue(self.take().ok)
        observe = self.runtime.observe
        def steal(*args):
            self.desktop.active = 0x123
            return observe(*args)
        with patch.object(self.runtime, "observe", steal):
            self.assertEqual(self.back().code, "UNCERTAIN_OUTCOME")
        self.assertEqual(self.runtime.attachments(), ())
        self.assertIsNone(self.broker.lease(self.sid)["holder"])
        self.assertIsNone(self.broker.history(self.sid)[1])

    def test_incomplete_retention_or_live_attachment_prevents_summon(self):
        with patch.object(self.runtime, "detach", return_value=None):
            self.assertEqual(self.take().code, "HOLD")
        self.assertEqual(self.desktop.moves, [])
        retain = self.runtime.retain
        with patch.object(self.runtime, "retain", side_effect=lambda s, b: dict(retain(s, b), state="WAKING")):
            self.assertEqual(self.take().code, "HOLD")
        self.assertEqual(self.desktop.moves, [])
        self.assertTrue(self.take().ok)

    def test_wrong_epoch_time_or_identity_in_observation_keeps_agent_fenced(self):
        self.assertTrue(self.take().ok)
        observe = self.runtime.observe
        for fields in (dict(authority_epoch=1), dict(captured_mono_ns=0),
                       dict(incarnation="other"), dict(observation_id=self.old.id)):
            with self.subTest(fields=fields), patch.object(self.runtime, "observe",
                    side_effect=lambda s, b: replace(observe(s, b), **fields)):
                self.assertEqual(self.back().code, "STALE_FRAME")
                self.assertEqual(self.runtime.attachments(), ())
                self.assertIsNone(self.broker.lease(self.sid)["holder"])

    def test_l0_6_refuses_missing_qualification_and_selects_only_receipted_mode(self):
        def make(receipt):
            return HumanHandoff(broker=self.broker, registry=self.registry, session_id=self.sid,
                                surface_id=self.surface, windows=self.handoff.windows, runtime=self.runtime,
                                lineage=self.journal, receipt=receipt)
        receipt = deepcopy(self.wl0)
        receipt["steps"]["L0-6"]["status"] = "NOT RUN"
        with self.assertRaisesRegex(HandoffRefused, "HOLD"):
            make(receipt)
        receipt = deepcopy(self.wl0)
        receipt["steps"]["L0-6"]["reattach"] = "relaunch-attach"
        with self.assertRaisesRegex(HandoffRefused, "HOLD"):
            make(receipt)
        receipt["focus_rules"]["human_cdp"]["reattach_mode"] = "relaunch-attach"
        self.handoff = make(receipt)
        self.assertTrue(self.take().ok)
        self.assertTrue(self.back().ok)
        self.assertIn(("connect", "relaunch-attach", 3), self.order)

    def test_window_and_handoff_use_same_provisioned_wl0_snapshot(self):
        projection = deepcopy(self.wl0)
        projection["steps"]["L0-6"]["reattach"] = "relaunch-attach"
        projection["focus_rules"]["human_cdp"]["reattach_mode"] = "relaunch-attach"
        path = self.root / "wl0.json"
        path.write_text(json.dumps(projection))
        windows = HandoffWindows("/fixture", self.registry.get_session, path, run=self.desktop.run)
        path.unlink()
        handoff = HumanHandoff(broker=self.broker, registry=self.registry, session_id=self.sid,
                               surface_id=self.surface, windows=windows, runtime=self.runtime, lineage=self.journal)
        self.assertEqual(handoff.mode, "relaunch-attach")
        self.assertEqual(self.desktop.moves, [])

    def test_malformed_or_unqualified_wl0_never_enables_handoff(self):
        from window import WindowRefused

        path = self.root / "wl0.json"
        for content in ("{", "null", json.dumps(dict(self.wl0, schema="other"))):
            path.write_text(content)
            with self.subTest(content=content[:10]):
                with self.assertRaises(WindowRefused) as refusal:
                    HandoffWindows("/fixture", self.registry.get_session, path, run=self.desktop.run)
                self.assertEqual(refusal.exception.code, "host_window_unavailable")
                with self.assertRaisesRegex(HandoffRefused, "^HOLD$"):
                    HumanHandoff(broker=self.broker, registry=self.registry, session_id=self.sid,
                                 surface_id=self.surface, windows=self.handoff.windows, runtime=self.runtime,
                                 lineage=self.journal, receipt=path)
        self.assertEqual(self.desktop.moves, [])

    def test_mutant_reloading_default_projection_loses_provisioned_qualification(self):
        mutant = mutate(HumanHandoff.__init__, "projection = windows.qualification_receipt()",
                        "projection = load_wl0()")
        with patch.object(HumanHandoff, "__init__", mutant), self.assertRaisesRegex(AssertionError, "relaunch-attach"):
            self.test_window_and_handoff_use_same_provisioned_wl0_snapshot()

    def test_c1_stamped_caller_and_duplicate_request(self):
        from control.core.control import CapabilityRegistry, Dispatcher, LaneIdentity, RequestStore
        caps = []
        for op in ("vp_human_take", "vp_human_return"):
            caps.append(dict(example("vp.capability"), tool=op, json_op=op, cli=op[3:].replace("_", "-")))
        dispatcher = Dispatcher(CapabilityRegistry(caps), RequestStore(self.root / "requests.sqlite"),
                                tools=self.handoff.handlers(), pace_risk=lambda *_: True,
                                lease_epoch=lambda *_: True)
        operator = LaneIdentity("operator", "fixture-operator")
        agent = LaneIdentity("agent", self.owner)
        spoofed = self.request("vp_human_take")
        self.assertEqual(dispatcher.dispatch_mailbox(spoofed, agent)["code"], "HUMAN_NEEDED")
        req = self.request("vp_human_take")
        reply = dispatcher.dispatch_mailbox(req, operator)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(dispatcher.dispatch_mailbox(req, operator), reply)
        self.assertEqual(len(self.desktop.moves), 1)
        self.assertEqual(dispatcher.dispatch_mailbox(self.request("vp_human_return"), agent)["code"], "NO_LEASE")
        self.assertTrue(dispatcher.dispatch_mailbox(self.request("vp_human_return"), operator)["ok"])

    def test_goal_freezes_and_resumes_with_return_observation(self):
        from rewind.machine import GoalMachine
        checkpoint = example("vp.rewind_receipt")
        checkpoint.update(op="checkpoint", goal_id="fixture-goal")
        checkpoint["budget"].update(attempts_used=0, rewinds_used=0)
        goal = GoalMachine.create(self.root / "goal.sqlite", goal_id="fixture-goal", session_id=self.sid,
                                  binding=self.broker.binding(self.surface), checkpoint=checkpoint, score=100,
                                  steps=3, duplicates=3, epsilon=10, action_quantum="0.01",
                                  observation_id=self.old.id, generation=1)
        self.handoff.goal = goal
        before = goal.status()
        self.assertTrue(self.take().ok)
        self.assertTrue(goal.replay()["human"])
        self.assertIsNone(goal.status()["observation"])
        result = self.back()
        self.assertTrue(result.ok)
        after = goal.replay()
        self.assertFalse(after["human"])
        self.assertEqual(after["observation"], result.result["fresh_observation_id"])
        self.assertEqual(after["authority_epoch"], self.broker.lease(self.sid)["authority_epoch"])
        self.assertEqual([before[k] for k in ("t", "r", "d")], [after[k] for k in ("t", "r", "d")])

    def exercise_retention(self, *, other_consumer):
        from functools import partial
        from perception.demand import DemandCoordinator
        from perception.stream import PerceptionStream
        from perception.stream.tests.test_stream import put_screen
        from human.channels import retain_agent_interest
        from vadapt import x11_ramcap

        root = Path(self.enterContext(TemporaryDirectory(dir="/dev/shm")))
        binding = self.broker.binding(self.surface)
        put_screen(root / "fixture.xwd", b"abc" * 32)
        source = x11_ramcap.SourceConfig(str(root), "fixture.xwd", 32, 1, "synthetic S4 RAM fixture")
        stream = PerceptionStream(binding, source, ring_slots=2, interval_ns=1000, ttl_ns=10**10,
                                  max_frame_bytes=4096, max_ring_bytes=8192, workspace_root=root / "rings")
        self.addCleanup(stream.close)
        obs = stream.wake()
        before = stream.status()
        def pause():
            stream.pause(obs.token)
            return True
        demand = DemandCoordinator(self.surface, interests=["VERIFIED"], authorize=lambda _: True, drain=pause)
        if other_consumer:
            self.enterContext(demand.register(example("vp.demand")))
        agent = demand.register(dict(example("vp.demand"), consumer=self.owner, demand_id="agent-read"))
        self.enterContext(agent)
        self.runtime.retain = partial(retain_agent_interest, demand=demand, stream=stream,
                                      withdraw=lambda: agent.__exit__(None, None, None), drain=lambda: True)
        self.assertTrue(self.take().ok)
        after = stream.status()
        self.assertEqual((after["ring_id"], after["reserved_bytes"]),
                         (before["ring_id"], before["reserved_bytes"]))
        state = self.broker.perception_state(self.surface)
        self.assertEqual(state["state"], "ASLEEP")
        self.assertEqual(state["obligations"], ["demand"] if other_consumer else [])
        return after["state"]

    def test_last_agent_demand_pauses_real_stream_and_retains_ring_charge(self):
        self.assertEqual(self.exercise_retention(other_consumer=False), "ASLEEP")

    def test_shared_recording_demand_keeps_real_producer_active(self):
        self.assertEqual(self.exercise_retention(other_consumer=True), "ACTIVE")

    def test_legacy_broker_session_still_acts_after_its_ordinary_return(self):
        for state, epoch in (("HUMAN", 1), ("AGENT", 2)):
            self.broker.human_lease(self.sid, state, epoch, "operator")
        lock = self.broker.try_acquire(self.owner, self.surface, self.inc, 3, "legacy-act")
        self.broker.release(self.owner, lock)

    def test_s4_ordinary_return_refuses_before_append_and_requires_fresh_readback(self):
        self.assertTrue(self.take().ok)
        log = self.root / "rt/events.jsonl"
        observe = self.runtime.observe

        def check_ordinary_return():
            before = log.read_bytes()
            lease = self.broker.lease(self.sid)
            with self.assertRaisesRegex(BrokerError, "^NO_LEASE$"):
                self.broker.human_lease(self.sid, "AGENT", lease["authority_epoch"], "operator")
            self.assertEqual(log.read_bytes(), before, "ordinary return appended to S4 journal")
            self.assertEqual(self.broker.lease(self.sid), lease)
            self.assert_input_fenced(lease["authority_epoch"])

        check_ordinary_return()
        self.broker.close()
        self.broker = self.handoff.broker = Broker(self.root / "rt")
        check_ordinary_return()

        def check_before_readback(*args):
            check_ordinary_return()
            return observe(*args)

        with patch.object(self.runtime, "observe", side_effect=check_before_readback):
            returned = self.back()
        self.assertTrue(returned.ok, returned.code)
        self.assertNotEqual(returned.result["fresh_observation_id"], self.old.id)
        epoch = returned.result["lease"]["epoch_after"]
        lock = self.broker.try_acquire(self.owner, self.surface, self.inc, epoch, "after-readback")
        self.broker.release(self.owner, lock)

    def test_mutant_ordinary_return_appends_to_s4_journal_without_preparation(self):
        bad = mutate(Broker.human_lease, 'if "handoff" in self._takes.get(session_id, {}) or self._pending_return(session_id):',
                     "if self._pending_return(session_id):")
        with patch.object(Broker, "human_lease", bad):
            with self.assertRaisesRegex(AssertionError, "ordinary return appended to S4 journal"):
                self.test_s4_ordinary_return_refuses_before_append_and_requires_fresh_readback()

    def test_restart_after_grant_rechecks_executor_and_quarantines_inflight_act(self):
        self.assertTrue(self.take().ok)
        self.assertTrue(self.back().ok)
        lock = self.broker.try_acquire(self.owner, self.surface, self.inc, 3, "in-flight")
        self.broker.dispatch(self.owner, lock)
        self.broker.close()
        self.broker = self.handoff.broker = HandoffBroker(self.root / "rt")
        self.assertEqual(self.back().code, "UNCERTAIN_OUTCOME")
        self.assertEqual(self.broker.lock_status(self.surface)["status"], "UNKNOWN")
        self.assertIsNone(self.broker.lease(self.sid)["holder"])
        self.proof = ("VERIFIED", "c" * 64)
        self.assertTrue(self.back().ok)
        self.assertEqual(len(self.journal.verify()), 2)

    def test_mutant_summon_before_fence(self):
        original = HumanHandoff._take
        mutant = mutate(original, "saved = self.windows.save_focus()",
                        "self.windows.summon(session)\n        saved = self.windows.save_focus()")
        observed = []
        self.desktop.on_move = lambda _: observed.append(self.broker.lease(self.sid)["state"])
        with patch.object(HumanHandoff, "_take", mutant):
            self.assertTrue(self.take().ok)
        self.assertTrue(observed)
        with self.assertRaises(AssertionError):
            self.assertEqual(observed[0], "HUMAN")

    def test_mutant_stay_attached(self):
        mutant = mutate(SessionRuntime.detach, "executor.detach()", "pass")
        with patch.object(SessionRuntime, "detach", mutant):
            self.assertEqual(self.take().code, "HOLD")
        self.assertFalse(self.ports[0].closed)
        self.assertEqual(self.desktop.moves, [])
        self.assertEqual(self.broker.lease(self.sid)["state"], "HUMAN")

    def test_mutant_skip_epoch_bump(self):
        self.assertTrue(self.take().ok)
        original = self.broker._write
        def broken(rec, **fields):
            if fields.get("op") == "prepare_human_return":
                fields["epoch_after"] = fields["epoch_before"]
            return original(rec, **fields)
        with patch.object(self.broker, "_write", broken):
            self.assertEqual(self.back().code, "NO_LEASE")
        row = json.loads((self.root / "rt/events.jsonl").read_bytes().splitlines()[-1])
        self.assertEqual(row["op"], "prepare_human_return")
        self.assertEqual(row["epoch_after"], row["epoch_before"])
        self.assertEqual(self.runtime.attachments(), ())

    def test_mutant_reuse_old_observation(self):
        self.assertTrue(self.take().ok)
        mutant = mutate(SessionRuntime.observe, "identifier = observed.id", "identifier = self._last()")
        with patch.object(SessionRuntime, "observe", mutant):
            self.assertEqual(self.back().code, "STALE_FRAME")
        self.assertTrue(any(method == "Accessibility.getFullAXTree" for method, _, _ in self.ports[-1].calls))
        self.assertIsNone(self.broker.lease(self.sid)["holder"])

    def test_mutant_auto_return_on_timeout(self):
        self.assertTrue(self.take().ok)
        mutant = mutate(HumanHandoff._call, 'if ctx["caller"] == "agent":',
                        'if ctx["caller"] == "agent" and self.clock() < 10**20:')
        with patch.object(HumanHandoff, "_call", mutant), patch.object(self.handoff, "clock", return_value=10**20):
            self.runtime._clock = self.handoff.clock
            result = self.handoff.execute(self.request("vp_human_return", "agent"))
        self.assertTrue(result.ok)
        with self.assertRaises(AssertionError):
            self.assertEqual(self.broker.lease(self.sid)["state"], "HUMAN")


def mutate(method, old, new):
    source = dedent(inspect.getsource(method))
    if source.count(old) != 1:
        raise AssertionError(f"mutation site changed in {method.__name__}")
    scope = {}
    exec(compile(source.replace(old, new), inspect.getfile(method), "exec"), method.__globals__, scope)
    return scope[method.__name__]
