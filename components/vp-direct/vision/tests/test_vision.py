import io
import inspect
import json
import sys
import textwrap
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic_ns
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from PIL import Image

DIRECT = Path(__file__).resolve().parents[2]
REPO = DIRECT.parents[1]
sys.path[:0] = [str(DIRECT), str(REPO / "agents"), str(DIRECT.parent / "wender-agent")]

from bus.broker import Broker
from channel.atspi import AtspiExecutor, HostIdentity, ObjectRef
from control.core.control import CapabilityRegistry, Dispatcher, LaneIdentity, RequestStore
from human import FreshObservation
from human.channels import retain_agent_interest
from frames.source import FrameSource, HostFrameState, Raster
from metaviewer_agents.perception.vcore import _BoundVision
from perception.demand import DemandCoordinator
from perception.lifecycle import LifecycleError, PerceptionLifecycle, RecoverableDemandCoordinator, RuntimeEvidence
from perception.stream import PerceptionStream
from perception.stream.tests.test_stream import put_screen
from rewind.machine import GoalMachine
from vadapt import x11_ramcap
from privacy import HostPrivacyEvidence, PrivacySentinel
from recorder.timeline import TimelinePosition, TimelineRecorder
from schemas.validation import ValidationError, validate_contract
from vision import VisionError, VisionFrames, VisionMode, local_adapters


def example(name):
    return json.loads((DIRECT / "schemas/tests/examples" / (name + "_v1.valid.json")).read_bytes())


class StreamFixture:
    """P3's owner-facing interface, with deterministic RGB custody evidence."""

    def __init__(self, binding):
        self.binding = binding
        self.generation = 0
        self.state = "OFF"
        self.ring = self.scrubbed = self.observation = None
        self.pixels = b"rgb" * 12

    def status(self):
        return dict(state=self.state, ring_id=self.ring, last_scrubbed_ring=self.scrubbed,
                    stream_generation=self.generation, reserved_bytes=4096 if self.ring else 0,
                    fresh_observation_id=self.observation)

    def wake(self):
        self.generation += 1
        self.scrubbed = self.ring
        self.ring = f"ring-{self.generation}"
        self.observation = f"observation-{self.generation}"
        self.state = "ACTIVE"
        return SimpleNamespace(observation_id=self.observation, stream_generation=self.generation,
                               token=SimpleNamespace(**self.binding, nonce=self.ring),
                               frame_sha256=sha256(self.pixels).hexdigest())

    def read(self, token, observation_id):
        assert self.state == "ACTIVE" and self.observation == observation_id
        assert token.nonce == self.ring
        return self.pixels

    def pause(self, token):
        assert token.nonce == self.ring
        self.state = "ASLEEP"
        self.observation = None


class ModeTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        broker = Broker(Path(temp.name) / "rt")
        self.addCleanup(broker.close)
        self.broker = broker
        self.binding = example("vp.surface_binding")
        self.stream = StreamFixture(self.binding)
        broker.bind("session", self.binding, lease_state="AGENT")
        agent = dict(example("vp.demand"), surface_id=self.binding["surface_id"],
                     consumer=self.binding["owner"], interest="VERIFIED", demand_id="agent")
        self.capture_demand = dict(agent, demand_id="vision-frame")
        self.recording_demand = dict(agent, demand_id="recording", consumer="recorder")
        coordinator = DemandCoordinator(self.binding["surface_id"], interests=["VERIFIED"],
                                         authorize=lambda row: row in (agent, self.capture_demand,
                                                                      self.recording_demand),
                                         drain=lambda: True)
        self.register = coordinator.register
        self.lifecycle = PerceptionLifecycle(
            broker, "session", self.binding, coordinator, agent, self.stream,
            qualified=lambda _: True, runtime=lambda: RuntimeEvidence("inc", (), 0),
            drain_agent=lambda _: self.mode.drain(),
        )
        self.state = HostFrameState(self.binding["surface_id"], "inc", 1, 1, "window",
                                    (0, 0, 4, 3), (4, 3), "xpra:7", False, "xpra")
        self.current = lambda _: self.state
        self.acquire = Mock(side_effect=self.raster)
        self.cdp = Mock(side_effect=self.raster)
        self.xwd = Mock(side_effect=self.raster)
        self.atspi = Mock(name="atspi", return_value=None)
        self.bind_frames()
        self.provider = SimpleNamespace(last_vserve_error=None, last_c2_payload=None,
                                        last_transport=None, close=Mock())
        self.seen_previous = []
        self.color = "red"
        self.transport = "vserve"
        self.fallback_error = None
        self.provider.perceive = Mock(side_effect=self.perceive)
        self.provider_factory = Mock(return_value=self.provider)
        self.planner = SimpleNamespace(plan_step=Mock(side_effect=self.plan))
        self.adapters = self.provider_factory, lambda: self.planner
        self.mode = self.make_mode()
        self.addCleanup(lambda: self.mode.drain())

    def bind_frames(self):
        self.source = FrameSource(self.current, {"xpra": self.acquire, "cdp": self.cdp, "xwd": self.xwd},
                                  self.register)
        self.frames = VisionFrames(self.source, self.current, self.capture_demand, atspi=self.atspi)

    def make_mode(self, *, fallback=False):
        return VisionMode(self.lifecycle, self.frames, self.adapters, session_origin_ns=monotonic_ns(),
                          capsule_id="capsule", allow_oneshot_fallback=fallback)

    def raster(self, state):
        image = Image.new("RGB", state.raster_size, "white")
        image.putpixel((0, 0), Image.new("RGB", (1, 1), self.color).getpixel((0, 0)))
        data = io.BytesIO()
        image.save(data, format="PNG")
        return Raster(state, {"xpra": "xpra", "cdp": "cdp_png", "xwd": "xwd"}[state.backend],
                      data.getvalue(), monotonic_ns(), "a" * 64)

    def perceive(self, ref, frame, *, prev):
        self.seen_previous.append(prev)
        self.assertEqual(ref["sha256"], sha256(frame.data).hexdigest())
        self.assertEqual(Path(ref["ramws_path"]).read_bytes(), frame.data)
        self.provider.last_transport = self.transport
        if self.fallback_error is not None:
            self.provider.last_vserve_error = self.fallback_error
        self.provider.last_c2_payload = dict(frame_sha256=ref["sha256"],
                                             changed=None if prev is None else prev != frame)
        return _BoundVision(dict(summary="fixture", changed_regions=[], elements=[]), ref["sha256"])

    def plan(self, observation, table, goal):
        ref = observation["frame_ref"]
        self.assertEqual(observation["schema"], "observation/v0.2")
        self.assertEqual(table["frame_sha256"], sha256(Path(ref["ramws_path"]).read_bytes()).hexdigest())
        self.assertEqual(goal, "inspect fixture")
        return ("fixture-candidate",)

    def call(self, op, *, args=None, dry_run=False, **context):
        if args is None:
            args = dict(session_id="session", goal="inspect fixture", steps=8) if op == "start" else {}
        tool = "vp_vision_" + op
        request = dict(schema="vp.control.req/v1", request_id=op, op=tool, args=args, dry_run=dry_run,
                       context=dict(dict(caller="agent", agent_instance=self.binding["owner"]), **context))
        return self.mode.execute(request)

    def start(self):
        result = self.call("start")
        self.assertTrue(result.ok, result)
        return result.result["observation_id"]

    def expect_refusal(self, code, observe):
        with self.assertRaises(VisionError) as caught:
            self.mode.analyze(observe)
        self.assertEqual(caught.exception.code, code)

    def test_start_wakes_and_stop_releases_only_vision(self):
        self.assertEqual(self.call("status").result["perception_state"], "OFF")
        observe = self.start()
        self.assertEqual(self.stream.generation, 1)
        self.assertEqual(self.start(), observe)
        self.assertEqual(self.stream.generation, 1)
        result = self.mode.analyze(observe)
        self.assertEqual(result["plan"], ("fixture-candidate",))
        self.assertEqual(result["transport"], "vserve")
        ref = self.planner.plan_step.call_args.args[0]["frame_ref"]
        self.assertFalse(Path(ref["ramws_path"]).exists())
        self.assertEqual(self.source._frames, {})
        self.assertFalse(self.call("stop").result["running"])
        self.provider.close.assert_called_once()
        self.assertEqual(self.call("status").result["perception_state"], "ACTIVE")
        self.expect_refusal("NO_LEASE", observe)

    def test_refuses_without_lease_or_before_start(self):
        self.expect_refusal("NO_LEASE", "retired")
        self.assertEqual(self.call("start", agent_instance="different-owner").code, "NO_LEASE")
        self.broker.human_lease("session", "HUMAN", self.binding["authority_epoch"], "operator")
        self.assertEqual(self.call("start").code, "NO_LEASE")
        self.assertEqual(self.stream.generation, 0)
        self.acquire.assert_not_called()
        self.provider_factory.assert_not_called()

    def test_failed_stop_fences_the_job_until_close_is_retried(self):
        observe = self.start()
        self.mode.analyze(observe)
        self.provider.close.side_effect = OSError("fixture close failed")
        self.assertEqual(self.call("stop").code, "HOLD")
        self.expect_refusal("NO_LEASE", observe)
        self.assertEqual(self.call("start").code, "HOLD")
        self.provider.close.side_effect = None
        self.assertTrue(self.call("stop").ok)
        self.assertEqual(self.start(), observe)

    def test_frozen_wire_scope_and_dry_run(self):
        self.assertEqual(self.call("start", args={"goal": "inspect fixture", "steps": 1}).code, "-32602")
        self.assertEqual(self.call("status", args={"session_id": "session"}).code, "-32602")
        self.assertEqual(self.call("start", authority_epoch=0).code, "identity_mismatch")
        self.assertFalse(self.call("start", dry_run=True).result["running"])
        self.assertEqual(self.stream.generation, 0)
        result = self.call("start", args=dict(capsule_id="capsule", goal="inspect fixture", steps=1))
        self.assertTrue(result.ok, result)
        observe = result.result["observation_id"]
        self.assertTrue(self.call("stop", dry_run=True).result["running"])
        self.assertIsNone(self.mode.analyze(observe)["changed"])
        self.expect_refusal("HOLD", observe)
        self.assertEqual(self.planner.plan_step.call_count, 1)

    def test_control_dispatcher_routes_bound_vision_handlers(self):
        entry = example("vp.capability")
        entries = [dict(entry, tool=tool, json_op=tool, cli=tool.removeprefix("vp_").replace("_", "-"))
                   for tool in self.mode.handlers()]
        dispatcher = Dispatcher(CapabilityRegistry(entries), RequestStore(self.workspace / "c1.sqlite"),
                                pace_risk=lambda *_: True, lease_epoch=lambda *_: True,
                                tools=self.mode.handlers())
        request = dict(schema="vp.control.req/v1", op="vp_vision_start", request_id="vision",
                       args=dict(session_id="session", goal="inspect fixture", steps=3),
                       context=dict(caller="agent", agent_instance="untrusted-claimed-owner"),
                       dry_run=False)
        lane = LaneIdentity("agent", self.binding["owner"])
        reply = dispatcher.dispatch_cli("vision-start", request, lane)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], {})
        self.assertEqual(self.call("status").result["observation_id"], self.stream.observation)
        self.assertEqual(dispatcher.dispatch_mailbox(request, lane), reply)
        self.assertEqual(self.stream.generation, 1)
        status = dict(request, op="vp_vision_status", request_id="status", args={})
        refused = dispatcher.dispatch_mailbox(status, lane)
        self.assertEqual(refused["code"], "HOLD")
        self.assertEqual(refused["not_verified"], ["SCHEMA_GAP: vision status result"])
        status_result = dict(refused, ok=True, code="OK", result=self.call("status").result)
        with self.assertRaises(ValidationError):
            validate_contract("vp.control.reply/v1", status_result)
        stop = dict(request, op="vp_vision_stop", request_id="stop", args={})
        self.assertEqual(dispatcher.dispatch_mailbox(stop, lane)["code"], "OK")
        self.assertFalse(self.call("status").result["running"])

    def test_change_is_tristate_and_geometry_starts_new_history(self):
        observe = self.start()
        self.assertIsNone(self.mode.analyze(observe)["changed"])
        self.assertIs(self.mode.analyze(observe)["changed"], False)
        self.color = "blue"
        self.assertIs(self.mode.analyze(observe)["changed"], True)
        self.state = replace(self.state, geometry_epoch=2)
        self.assertIsNone(self.mode.analyze(observe)["changed"])
        self.assertIsNone(self.seen_previous[-1])

    def test_pause_drains_and_rejects_pre_sleep_observation(self):
        observe = self.start()
        self.mode.analyze(observe)
        receipt = self.lifecycle.vp_perception_call(dict(op="sleep", session_id="session", reason="test"))
        self.assertEqual(receipt["to"], "ASLEEP")
        self.assertIsNone(self.mode._history)
        self.provider.close.assert_called_once()
        self.expect_refusal("NO_LEASE", observe)
        fresh = self.start()
        self.assertNotEqual(fresh, observe)
        self.expect_refusal("STALE_FRAME", observe)
        self.assertIsNone(self.mode.analyze(fresh)["changed"])
        self.assertEqual(self.provider_factory.call_count, 2)
        self.assertEqual(self.acquire.call_count, 2)

    def test_sleep_waits_for_an_inflight_provider_and_refuses_late_output(self):
        observe = self.start()
        entered, release = Event(), Event()

        def blocked_perception(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.perceive(*args, **kwargs)

        def sleep():
            return self.lifecycle.vp_perception_call(
                dict(op="sleep", session_id="session", reason="test"))

        self.provider.perceive.side_effect = blocked_perception
        with ThreadPoolExecutor(max_workers=2) as workers:
            analyzing = workers.submit(self.mode.analyze, observe)
            try:
                self.assertTrue(entered.wait(2))
                sleeping = workers.submit(sleep)
                with self.lifecycle._admission:
                    self.assertTrue(self.lifecycle._admission.wait_for(
                        lambda: not self.lifecycle._admitting, timeout=2))
                self.assertFalse(sleeping.done())
                self.provider.close.assert_not_called()
            finally:
                release.set()
            with self.assertRaises(VisionError) as caught:
                analyzing.result(timeout=2)
            self.assertEqual(caught.exception.code, "NO_LEASE")
            self.assertTrue(sleeping.result(timeout=2)["complete"])
        self.planner.plan_step.assert_not_called()
        self.provider.close.assert_called_once()

    def test_authority_epoch_drift_blocks_publication(self):
        observe = self.start()
        self.mode.analyze(observe)

        def changed_authority(*args, **kwargs):
            result = self.perceive(*args, **kwargs)
            self.state = replace(self.state, authority_epoch=2)
            return result

        self.provider.perceive.side_effect = changed_authority
        self.expect_refusal("STALE_FRAME", observe)
        self.assertIsNone(self.mode._history)
        self.assertEqual(self.planner.plan_step.call_count, 1)

    def check_epoch_history_reset(self):
        observe = self.start()
        self.assertIsNone(self.mode.analyze(observe)["changed"])
        self.assertIs(self.mode.analyze(observe)["changed"], False)

        self.broker.human_lease("session", "HUMAN", self.state.authority_epoch, "operator")
        self.broker.human_lease("session", "AGENT", self.state.authority_epoch + 1, "operator")
        rebound = self.broker.binding(self.state.surface_id)
        self.state = replace(self.state, authority_epoch=rebound["authority_epoch"])
        # Exercise R2's own fence even if the rebound owner issues an identical ID.
        self.mode._lifecycle = SimpleNamespace(binding=rebound, broker=self.broker, session_id="session",
                                               stream=self.stream, run_agent=lambda _, __, perform: perform())
        self.assertIsNone(self.mode.analyze(observe)["changed"])
        self.assertIsNone(self.seen_previous[-1])
        self.assertIs(self.mode.analyze(observe)["changed"], False)

    def test_authority_epoch_rebind_starts_new_comparison_history(self):
        self.check_epoch_history_reset()

    def test_mutant_reuses_comparison_history_across_authority_epochs(self):
        source = textwrap.dedent(inspect.getsource(VisionMode._analyze))
        condition = "self._history[:2] == (observation_id, state)"
        self.assertEqual(source.count(condition), 1)
        mutant = source.replace(condition, "(self._history[0], replace(self._history[1], "
                                "authority_epoch=state.authority_epoch)) == (observation_id, state)")
        namespace = dict(VisionMode._analyze.__globals__, replace=replace)
        exec(compile(mutant, __file__, "exec"), namespace)
        with patch.object(VisionMode, "_analyze", namespace["_analyze"]):
            with self.assertRaisesRegex(AssertionError, "False is not None"):
                self.check_epoch_history_reset()

    def bind_hidden_host(self, *, window_state=0):
        self.state = replace(self.state, display=":0", hidden=True, backend="cdp", cdp_target_id="tab")
        state = self.state
        session = example("vp.session")
        session.update(session_id="session", capsule_id="capsule", display=state.display,
                       incarnation=state.incarnation, cdp_target_id=state.cdp_target_id,
                       window=dict(xid="0x18", pid=24, witness=state.window_witness),
                       lease=dict(holder=self.binding["owner"], authority_epoch=state.authority_epoch))
        window = ObjectRef(":1.10", "/window")
        identity = HostIdentity(state.display, session["window"]["pid"], session["window"]["xid"],
                                state.window_witness, state.incarnation,
                                state.authority_epoch, window, input_allowed=False, hidden=state.hidden)
        request = dict(example("vp.channel.req"), session_id=session["session_id"],
                       capsule_id=session["capsule_id"], incarnation=state.incarnation,
                       authority_epoch=state.authority_epoch, backend="atspi", dry_run=False,
                       require_vision_witness=False, target=dict(window_id=identity.xid))
        request.pop("observation_id")
        children = "org.a11y.atspi.Accessible.GetChildren"
        replies = {
            ("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root", children, ()): [(window.bus, "/app")],
            ("org.freedesktop.DBus", "/org/freedesktop/DBus",
             "org.freedesktop.DBus.GetConnectionUnixProcessID", (window.bus,)): identity.pid,
            (window.bus, "/app", children, ()): [(window.bus, window.path)],
            (window.bus, window.path, "org.a11y.atspi.Accessible.GetState", ()): [window_state, 0],
            (window.bus, window.path, "org.a11y.atspi.Accessible.GetRoleName", ()): "push button",
            (window.bus, window.path, "org.freedesktop.DBus.Properties.Get",
             ("org.a11y.atspi.Accessible", "Name")): "Fixture button",
            (window.bus, window.path, children, ()): [],
        }
        transport = Mock()
        transport.call.side_effect = lambda bus, path, method, sig, args, reply, deadline: (
            replies[bus, path, method, args])

        def lookup(session_id):
            if session_id == session["session_id"] and self.state == state:
                return identity

        executor = AtspiExecutor(lookup, session_env={}, transport=transport)
        self.addCleanup(executor.close)
        observations = []

        def observe(frame_state):
            self.assertEqual(frame_state, state)
            reply, snapshot = executor.observe(request, session=session)
            observations.append((reply, snapshot))
            return snapshot

        self.atspi.side_effect = observe
        return observations

    def provision_host_privacy(self, *, record_gap=None, request_human=None):
        self.host_evidence = HostPrivacyEvidence(self.state.privacy_binding(), 1, login_active=False,
                                                 reauthenticating=False, window_active=True, input_drained=True,
                                                 covers_capture=True, atspi_roles=("window", "push button"))
        self.read_privacy = Mock(side_effect=lambda _: self.host_evidence)
        self.host_gap = Mock(side_effect=record_gap)
        self.host_handoff = Mock(side_effect=request_human)
        self.frames = VisionFrames.from_host_ports(
            self.current, {"cdp": self.cdp, "xwd": self.xwd}, self.register, self.capture_demand,
            binding=self.host_evidence.binding, read_privacy_evidence=self.read_privacy,
            record_gap=self.host_gap, request_human=self.host_handoff, atspi=self.atspi,
        )
        self.mode = self.make_mode()

    def test_hidden_host_uses_s5_cdp_and_bound_atspi(self):
        observations = self.bind_hidden_host()
        self.provision_host_privacy()
        trace = Mock()
        trace.attach_mock(self.read_privacy, "metadata")
        trace.attach_mock(self.atspi, "atspi")
        trace.attach_mock(self.cdp, "cdp")

        result = self.mode.analyze(self.start())

        (reply, snapshot), = observations
        self.assertEqual(reply["code"], "OK")
        self.assertEqual(snapshot["atspi_capture"], "polling_only")
        self.assertEqual([node["name"] for node in snapshot["nodes"]], ["Fixture button"])
        self.assertEqual(result["atspi_sha256"], snapshot["ax_sha256"])
        metadata = call.metadata(self.host_evidence.binding)
        self.assertEqual(trace.mock_calls[:6], [metadata, call.atspi(self.state), metadata,
                                               metadata, call.cdp(self.state), metadata])
        self.cdp.assert_called_once_with(self.state)
        self.atspi.assert_called_once_with(self.state)
        self.acquire.assert_not_called()
        self.xwd.assert_not_called()

    def test_hidden_host_without_a2_holds_before_raster(self):
        observations = self.bind_hidden_host()
        self.expect_refusal("HOLD", self.start())
        self.assertFalse(observations)
        self.acquire.assert_not_called()
        self.cdp.assert_not_called()
        self.xwd.assert_not_called()
        self.provider_factory.assert_not_called()
        self.planner.plan_step.assert_not_called()

    def test_hidden_host_with_retired_atspi_object_refuses_before_raster(self):
        observations = self.bind_hidden_host(window_state=1 << 6)
        self.provision_host_privacy()
        self.expect_refusal("E_PRODUCER_ABSENT", self.start())
        (reply, snapshot), = observations
        self.assertEqual(reply["code"], "ATSPI_RETIRED_OBJECT")
        self.assertIsNone(snapshot)
        self.acquire.assert_not_called()
        self.cdp.assert_not_called()
        self.xwd.assert_not_called()
        self.provider_factory.assert_not_called()

    def check_login_blocks_host_reads(self):
        self.expect_refusal("HUMAN_NEEDED", self.start())
        self.atspi.assert_not_called()
        self.cdp.assert_not_called()
        self.provider_factory.assert_not_called()
        self.planner.plan_step.assert_not_called()
        self.host_gap.assert_called_once()
        self.host_handoff.assert_called_once_with("login")

    def test_host_login_blocks_atspi_raster_and_planning_before_acquisition(self):
        self.bind_hidden_host()
        self.provision_host_privacy()
        self.host_evidence = replace(self.host_evidence, reauthenticating=True)
        self.check_login_blocks_host_reads()

    def check_login_holds_lifecycle_and_seals_timeline(self, *, reauth_during_raster=False):
        self.bind_hidden_host()
        recorder = TimelineRecorder(surface_id=self.binding["surface_id"], capsule_id="capsule",
                                    incarnation=self.binding["incarnation"], register=self.register,
                                    verify_ram_frame=self.fail)

        def position(when):
            return TimelinePosition(when, "capsule", self.binding["incarnation"])

        def handoff(reason):
            return self.broker.human_needed(self.binding["owner"], "session", "goal", reason)

        self.provision_host_privacy(
            record_gap=lambda active, when: recorder.set_gap(position(when), "privacy", active=active),
            request_human=handoff,
        )
        with recorder.recording(self.recording_demand):
            observation = self.start()
            self.mode.analyze(observation)
            if reauth_during_raster:
                def reauthenticate(state):
                    staged = self.raster(state)
                    self.host_evidence = replace(self.host_evidence, revision=2, reauthenticating=True)
                    return staged

                self.cdp.side_effect = reauthenticate
            else:
                self.host_evidence = replace(self.host_evidence, revision=2, login_active=True)

            self.expect_refusal("HUMAN_NEEDED", observation)
            self.assertFalse(recorder.add_atspi(position(monotonic_ns()), atspi_capture="polling_only",
                                                text="synthetic-login-canary"), "recorder accepted protected feed")
            self.assertEqual([row["op"] for row in recorder.snapshot()["events"]], ["privacy_gap_start"])
            self.assertEqual(self.call("status").code, "HUMAN_NEEDED", "P4 left vision active")
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                self.lifecycle.read_frame(observation)
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                self.lifecycle.wake_for_verified()

            self.host_evidence = replace(self.host_evidence, revision=3, login_active=False,
                                         reauthenticating=False)
            self.assertEqual(self.call("start").code, "HUMAN_NEEDED")
            self.expect_refusal("HUMAN_NEEDED", observation)
            acquisitions = 2 if reauth_during_raster else 1
            self.assertEqual(self.atspi.call_count, acquisitions)
            self.assertEqual(self.cdp.call_count, acquisitions)
            self.assertFalse(self.frames._source._frames)
            self.provider.perceive.assert_called_once()
            self.planner.plan_step.assert_called_once()
            self.provider.close.assert_called_once()
            self.host_handoff.assert_called_once_with("login")
            self.assertEqual(self.broker.lease("session")["state"], "AGENT")
            journal = (self.workspace / "rt" / "events.jsonl").read_bytes()
            notices = [row for row in map(json.loads, journal.splitlines()) if row.get("rec") == "HUMAN_NEEDED"]
            self.assertEqual([row["reason"] for row in notices], ["login"])
            self.assertNotIn(b"synthetic-login-canary", journal)

    def test_login_holds_p4_and_r2_and_opens_u2a_gap(self):
        self.check_login_holds_lifecycle_and_seals_timeline()

    def test_reauthentication_during_raster_holds_and_discards_staged_frame(self):
        self.check_login_holds_lifecycle_and_seals_timeline(reauth_during_raster=True)

    def assert_crash_after_gap_is_fenced(self):
        self.bind_hidden_host()
        recorder = TimelineRecorder(surface_id=self.state.surface_id, capsule_id="capsule",
                                    incarnation=self.state.incarnation, register=self.register,
                                    verify_ram_frame=self.fail)
        class Interrupted(BaseException):
            pass

        def interrupt(active, when):
            recorder.set_gap(TimelinePosition(when, "capsule", self.state.incarnation), "privacy", active=active)
            raise Interrupted

        self.provision_host_privacy(record_gap=interrupt)
        observation = self.start()
        self.host_evidence = replace(self.host_evidence, revision=2, reauthenticating=True)
        with recorder.recording(self.recording_demand), self.assertRaises(Interrupted):
            self.mode.analyze(observation)
        self.assertEqual([r["op"] for r in recorder.snapshot()["events"]], ["privacy_gap_start"])
        self.host_handoff.assert_not_called()
        self.broker.close()
        reopened = Broker(self.workspace / "rt")
        self.addCleanup(reopened.close)
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            self.frames.provision_lifecycle(
                reopened, "session", self.binding, self.lifecycle.demand, self.lifecycle._agent_demand,
                self.stream, qualified=self.fail, runtime=self.fail, drain_agent=self.fail)
        self.atspi.assert_not_called()
        self.cdp.assert_not_called()
        self.planner.plan_step.assert_not_called()

    def test_owner_loss_after_u2a_gap_before_broker_notice(self):
        self.assert_crash_after_gap_is_fenced()

    def test_mutant_missing_write_before_gap_is_killed(self):
        with patch.object(PerceptionLifecycle, "hold_for_privacy", return_value=None):
            with self.assertRaisesRegex(AssertionError, 'does not match "HOLD"'):
                self.assert_crash_after_gap_is_fenced()

    def test_authorized_handback_resumes_same_goal_at_ready(self):
        self.bind_hidden_host()
        self.provision_host_privacy()
        old = self.start()
        checkpoint = example("vp.rewind_receipt")
        checkpoint.update(goal_id="g", op="checkpoint", confirmed_tick=0)
        checkpoint["budget"].update(attempts_limit=10, attempts_used=0, rewinds_used=0)
        goal = GoalMachine.create(self.workspace / "goal", goal_id="g", session_id="session",
                                  binding=self.binding, checkpoint=checkpoint, score=100, steps=2,
                                  duplicates=3, epsilon=1, action_quantum="0.01",
                                  observation_id=old, generation=1)
        self.lifecycle.bind_goal(goal)
        goal.propose("fixture.a1", {"value": 1}, observation=old)
        goal.uncertain("fixture.a1")
        self.host_evidence = replace(self.host_evidence, revision=2, login_active=True)
        self.expect_refusal("HUMAN_NEEDED", old)
        self.assertEqual(goal.reconcile("fixture.a1", "unknown", "reconciled", 1), "HUMAN_NEEDED")
        take = self.broker.fence("session", self.state.authority_epoch, "login", {})
        goal.human_lease(take)
        self.assertEqual(self.call("start").code, "HUMAN_NEEDED")
        self.expect_refusal("HUMAN_NEEDED", old)

        def drain_old_stream():
            self.stream.pause(self.lifecycle._observation.token)
            return self.mode.drain()

        retained = retain_agent_interest(
            dict(state="HUMAN", session_id="session"), self.broker.binding(self.state.surface_id),
            demand=self.lifecycle.demand, stream=self.stream, withdraw=self.lifecycle._cancel_demand,
            drain=drain_old_stream)
        self.assertEqual(self.broker.perception_transition(retained)["perception"]["state"], "ASLEEP")
        lease = self.broker.prepare_return("session", take["lease"]["epoch_after"])
        self.binding = self.broker.binding(self.state.surface_id)
        self.state = replace(self.state, authority_epoch=lease["epoch_after"])
        self.bind_hidden_host()
        self.provision_host_privacy()
        # U3a returned a channel observation. P3 issues its own observation
        # from the new epoch's bound producer only after the A2 check.
        put_screen(self.workspace / "screen", b"rgb" * 32)
        stream = PerceptionStream(self.binding, x11_ramcap.SourceConfig(
            str(self.workspace), "screen", 32, 1, "fixture"), ring_slots=2, interval_ns=1000,
            ttl_ns=10**10, max_frame_bytes=4096, max_ring_bytes=8192, workspace_root=self.workspace / "rings")
        self.addCleanup(stream.close)
        fresh = FreshObservation("session", self.state.surface_id, self.state.incarnation,
                                 self.state.authority_epoch, "returned-channel-observation", monotonic_ns(), 1)
        returned = self.broker.record_return(lease)
        goal.human_lease(returned, observation_id=fresh.observation_id, generation=fresh.generation)
        self.broker.finish_return(returned, fresh)
        demand = RecoverableDemandCoordinator(self.workspace / "returned.sqlite", self.binding,
                                              interests=["VERIFIED"], authorize=lambda _: True, drain=lambda: True)
        self.addCleanup(demand.retire_owner)
        readback = Mock(side_effect=stream.wake)
        def provision():
            return self.frames.provision_lifecycle(
                self.broker, "session", self.binding, demand, self.lifecycle._agent_demand, stream,
                recover_goal=goal, recover_observation=readback, qualified=lambda _: True,
                runtime=lambda: RuntimeEvidence("inc", (), 0), drain_agent=lambda _: self.mode.drain())

        self.host_evidence = replace(self.host_evidence, covers_capture=None)
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            provision()
        readback.assert_not_called()
        self.cdp.assert_not_called()
        self.atspi.assert_not_called()
        self.planner.plan_step.assert_not_called()
        # A change from UNKNOWN to CLEAR at the same revision is stale.
        self.host_evidence = replace(self.host_evidence, covers_capture=True)
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            provision()
        readback.assert_not_called()
        self.host_evidence = replace(self.host_evidence, revision=3)
        with patch.object(self.frames, "confirm_privacy_clear", return_value=False) as missing_clear:
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                provision()
            missing_clear.assert_called_once_with(self.binding)
        readback.assert_not_called()
        self.lifecycle = provision()
        self.mode = self.make_mode()
        readback.assert_called_once_with()
        self.assertEqual(goal.replay()["phase"], "READY")
        current = self.start()
        self.assertNotEqual(current, fresh.observation_id)
        self.assertEqual(goal.replay()["observation"], current)
        self.assertEqual(self.mode.analyze(current)["plan"], ("fixture-candidate",))
        self.planner.plan_step.assert_called_once()
        self.assertEqual(self.broker.perception_state(self.state.surface_id)["authority_epoch"], lease["epoch_after"])

    def test_unknown_host_privacy_refuses_observation_without_fabricating_login(self):
        self.bind_hidden_host()
        self.provision_host_privacy()
        self.host_evidence = replace(self.host_evidence, covers_capture=None)
        observation = self.start()
        self.expect_refusal("HOLD", observation)
        self.assertTrue(self.call("status").result["running"])
        self.host_handoff.assert_not_called()
        self.atspi.assert_not_called()
        self.cdp.assert_not_called()

    def test_failed_privacy_cleanup_leaves_p4_held_and_stop_can_retry_close(self):
        self.bind_hidden_host()
        self.provision_host_privacy()
        observation = self.start()
        self.mode.analyze(observation)
        self.host_evidence = replace(self.host_evidence, revision=2, login_active=True)
        self.provider.close.side_effect = OSError("fixture close failed")
        self.expect_refusal("HOLD", observation)
        self.assertEqual(self.call("status").code, "HUMAN_NEEDED")
        self.assertEqual(self.call("stop", dry_run=True).code, "HUMAN_NEEDED")
        self.provider.close.assert_called_once()

        self.provider.close.side_effect = None
        self.assertEqual(self.call("stop").code, "HUMAN_NEEDED")
        self.assertEqual(self.provider.close.call_count, 2)
        self.assertEqual(self.call("start").code, "HUMAN_NEEDED")
        self.atspi.assert_called_once()
        self.cdp.assert_called_once()

    def test_dropped_privacy_hold_is_still_fenced_by_broker_notice(self):
        # Defence in depth: with hold_for_privacy disabled, the durable broker login
        # notice alone keeps P4 from leaving vision active. The bypass before the
        # notice exists is killed by test_mutant_s5_skips_bound_owner_before_notice.
        with patch.object(PerceptionLifecycle, "hold_for_privacy", return_value=None) as hold:
            self.check_login_holds_lifecycle_and_seals_timeline()
            self.assertEqual(hold.call_args_list, [call(before_notice=True), call()])

    def test_mutant_drops_gap_fails_recorder_oracle(self):
        with patch.object(TimelineRecorder, "set_gap", return_value=False) as gap:
            with self.assertRaisesRegex(AssertionError, "recorder accepted protected feed"):
                self.check_login_holds_lifecycle_and_seals_timeline()
            gap.assert_called_once()

    def test_mutant_reads_atspi_before_privacy_fails_zero_acquisition_oracle(self):
        self.bind_hidden_host()
        self.provision_host_privacy()
        self.host_evidence = replace(self.host_evidence, login_active=True)
        observe = PrivacySentinel.observe

        def read_before_gate(sentinel, acquire, publish):
            if self.atspi.call_count == 0:
                self.atspi(self.state)
            return observe(sentinel, acquire, publish)

        with patch.object(PrivacySentinel, "observe", read_before_gate):
            with self.assertRaisesRegex(AssertionError, "Expected 'atspi' to not have been called"):
                self.check_login_blocks_host_reads()

    def check_mixed_revision_refused(self):
        self.bind_hidden_host()
        self.provision_host_privacy()
        before = self.host_evidence
        after = replace(before, revision=2)
        self.read_privacy.side_effect = lambda _: before if self.read_privacy.call_count <= 2 else after
        self.expect_refusal("STALE_FRAME", self.start())
        self.atspi.assert_called_once_with(self.state)
        self.cdp.assert_called_once_with(self.state)
        self.provider_factory.assert_not_called()
        self.planner.plan_step.assert_not_called()
        self.assertFalse(self.frames._source._frames)

    def test_revision_change_between_atspi_and_raster_blocks_mixed_readback(self):
        self.check_mixed_revision_refused()

    def test_mutant_combines_atspi_and_raster_from_different_privacy_revisions(self):
        source = textwrap.dedent(inspect.getsource(VisionFrames._capture_frame))
        check = 'require(current == atspi_metadata, "STALE_FRAME")'
        self.assertEqual(source.count(check), 1)
        namespace = dict(VisionFrames._capture_frame.__globals__)
        exec(compile(source.replace(check, "pass"), __file__, "exec"), namespace)
        with patch.object(VisionFrames, "_capture_frame", namespace["_capture_frame"]):
            with self.assertRaisesRegex(AssertionError, "VisionError not raised"):
                self.check_mixed_revision_refused()

    def test_hidden_xwd_and_missing_atspi_block_before_capture(self):
        self.state = replace(self.state, display=":0", hidden=True, backend="xwd")
        self.assertEqual(self.call("start").code, "DISPLAY_UNSUPPORTED")
        self.bind_hidden_host()
        self.atspi.side_effect = None
        self.provision_host_privacy()
        self.expect_refusal("E_PRODUCER_ABSENT", self.start())
        self.xwd.assert_not_called()
        self.cdp.assert_not_called()
        self.acquire.assert_not_called()
        self.provider_factory.assert_not_called()

    def test_explicit_fallback_records_fresh_reason_only(self):
        self.mode = self.make_mode(fallback=True)
        observe = self.start()
        self.transport = "oneshot"
        self.fallback_error = SimpleNamespace(code="unavailable")
        result = self.mode.analyze(observe)
        self.assertEqual((result["transport"], result["fallback_from"], result["fallback_reason"]),
                         ("oneshot", "vserve", "unavailable"))
        self.expect_refusal("HOLD", observe)
        self.fallback_error = SimpleNamespace(code="identity_mismatch")
        self.expect_refusal("HOLD", observe)
        self.assertEqual(self.planner.plan_step.call_count, 1)

    def test_refuses_silent_oneshot(self):
        self.mode = self.make_mode(fallback=True)
        observe = self.start()
        self.transport = "oneshot"
        self.expect_refusal("HOLD", observe)
        self.planner.plan_step.assert_not_called()

    def test_mutant_silent_fallback_reaches_planner_and_fails_oracle(self):
        self.mode = self.make_mode(fallback=True)
        observe = self.start()
        self.transport = "oneshot"

        def oracle():
            self.expect_refusal("HOLD", observe)
            self.planner.plan_step.assert_not_called()

        oracle()

        def silent_provider(ref, frame, previous):
            return self.provider.perceive(ref, frame, prev=previous), None, None

        with patch.object(self.mode, "_perceive", silent_provider):
            with self.assertRaises(AssertionError):
                oracle()
        self.planner.plan_step.assert_called_once()

    def test_local_adapter_factories_pin_dependencies(self):
        with (patch("metaviewer_agents.perception.vcore.VCorePerceptionAdapter") as vcore,
              patch("metaviewer_agents.wender_bridge.WenderSubprocessAdapter") as wender):
            perception, planner = local_adapters(ve_root="vision", wender_root="planner", timeout=2)
            vcore.assert_not_called()
            wender.assert_not_called()
            from vision.admission import VisionAdmissionError
            import os

            provider = perception()
            with patch.object(provider, "_call", side_effect=VisionAdmissionError("saturated")):
                with self.assertRaises(VisionAdmissionError):
                    provider.perceive({}, None, prev=None)
            vcore.assert_not_called()
            child = vcore.return_value._vserve_proc
            child.poll.return_value = 0
            grant = dict(front="MAIN", pid=os.getpid(), lease_id="fixture")
            with patch.object(provider, "_call", side_effect=[grant, {}]) as pool:
                self.assertIs(provider.perceive({}, None, prev=None), vcore.return_value.perceive.return_value)
            self.assertEqual([call.args[0] for call in pool.call_args_list], ["acquire", "release"])
            vcore.return_value.close.assert_called_once()
            self.assertIs(planner(), wender.return_value)
            vcore.assert_called_once_with("vision", timeout=2, verify_lock=True, fallback_oneshot=False,
                                          marks_out=None, ocr_marks=False, tessdata_prefix=None)
            wender.assert_called_once_with(wender_root="planner", timeout=2, verify_lock=True)
