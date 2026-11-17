from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import inspect
import json
from pathlib import Path
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

TESTS = Path(__file__).resolve().parent
DIRECT = TESTS.parents[2]
sys.path.insert(0, str(DIRECT))

from bus.broker import Broker
from channel import core
from channel.core import AtspiAction, ChannelCore, ChannelRefused, Observation, RequestStore, TargetContext
from schemas.validation import validate_contract


AX = sha256(b"fixture stable AX tree").hexdigest()
POST_AX = sha256(b"fixture AX after click").hexdigest()
FRAME = sha256(b"fixture frame").hexdigest()
AGENT = "fixture-agent"
SURFACE = "fixture-surface"
NODE = "unique-fixture-node"


def example(name):
    return json.loads((DIRECT / "schemas/tests/examples" / f"{name}_v1.valid.json").read_bytes())


def variant(module, symbol, *edits):
    source = inspect.getsource(module)
    for before, after in edits:
        if source.count(before) != 1:
            raise AssertionError("Mutant must change one known source site")
        source = source.replace(before, after)
    namespace = dict(vars(module))
    exec(compile(source, module.__file__, "exec"), namespace)
    return namespace[symbol]


class ChannelTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=TESTS)
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.broker = Broker(self.root)
        self.addCleanup(self.broker.close)
        self.path = self.root / "requests.sqlite3"
        self.store = RequestStore(self.path)
        session = dict(example("vp.session"), risk_ceiling="irreversible")
        self.target = TargetContext(session, SURFACE, "host")
        binding = dict(example("vp.surface_binding"), surface_id=SURFACE, owner=AGENT,
                       incarnation=session["incarnation"])
        self.broker.bind(session["session_id"], binding, lease_state="AGENT")
        self.observation = Observation(session["session_id"], session["capsule_id"], SURFACE,
                                       session["incarnation"], 1, "o-fixture", "cdp", AX, (NODE,), FRAME)
        self.wl0 = json.loads((DIRECT / "config/wl0.json").read_bytes())
        self.performed = {name: Mock(side_effect=self.perform_action) for name in ("cdp", "atspi", "vision")}
        self.backends = {name: Mock(return_value=fn) for name, fn in self.performed.items()}
        self.risk_for = Mock(return_value="observar")
        self.authorize = Mock(return_value=True)
        self.channel = self.build()

    def build(self, cls=ChannelCore, store=None):
        return cls(broker=self.broker, target_for=lambda _: self.target,
                   observation_for=lambda *_: self.observation, backends=self.backends,
                   wl0=self.wl0, request_store=self.store if store is None else store,
                   risk_for=self.risk_for, authorize=self.authorize,
                   fallback_codes={('cdp', 'DISPLAY_UNSUPPORTED'), ('cdp', 'identity_mismatch'),
                                   ('cdp', 'UNCERTAIN_OUTCOME')})

    def request(self, **changes):
        value = dict(example("vp.channel.req"), op="act", action="click", dry_run=False,
                     target={"cdp_target_id": "tab-fixture", "ax_stable_sha256": AX},
                     require_vision_witness=False)
        value.update(changes)
        return value

    def send(self, request=None, *, channel=None, **changes):
        channel = self.channel if channel is None else channel
        reply = channel.dispatch(self.request(**changes) if request is None else request, agent_id=AGENT)
        validate_contract("vp.channel.reply/v1", reply)
        return reply

    def perform_action(self, lock_id):
        self.assertIsInstance(lock_id, str)
        lock = self.broker.lock_status(SURFACE)
        self.assertEqual((lock["lock_id"], lock["status"]), (lock_id, "sent"))
        return {"schema": "vp.channel.reply/v1", "ok": True, "code": "OK",
                "ax_sha256": POST_AX, "frame_sha256": FRAME}

    def assert_no_backend(self):
        for backend in self.backends.values():
            backend.assert_not_called()

    def use_private_fallback(self, code="DISPLAY_UNSUPPORTED"):
        self.target = replace(self.target, surface_kind="private", private_backends=frozenset({"cdp", "vision"}),
                              session=dict(self.target.session, display="xpra:7"))
        self.backends["cdp"].side_effect = ChannelRefused(code)

    def test_closed_envelope(self):
        self.assertEqual(self.send(unexpected="value")["code"], "-32602")
        self.assertEqual(self.send(target={"unexpected": True})["code"], "-32602")
        self.assert_no_backend()

    def check_epoch_refused(self, channel=None):
        self.observation = replace(self.observation, authority_epoch=0)
        self.assertEqual(self.send(channel=channel, authority_epoch=0, dry_run=True)["code"], "NO_LEASE")
        self.assert_no_backend()

    def test_stale_authority_epoch_refused(self):
        self.check_epoch_refused()

    def test_wrong_incarnation_and_ambiguous_node_refused(self):
        self.assertEqual(self.send(incarnation="retired")["code"], "identity_mismatch")
        self.observation = replace(self.observation, node_matches=(NODE, NODE))
        self.assertEqual(self.send(request_id="ambiguous")["code"], "identity_mismatch")
        self.assert_no_backend()

    def test_human_mode_refused(self):
        self.broker.human_lease("s-fixture", "HUMAN", 1, "operator")
        self.assertEqual(self.send()["code"], "NO_LEASE")
        self.assert_no_backend()

    def test_dry_run_leaves_no_dispatch_and_returns_a_plan_verdict(self):
        self.assertTrue(self.send(dry_run=True)["ok"])
        self.performed["cdp"].assert_not_called()
        self.assertIsNone(self.broker.lock_status(SURFACE))
        self.assertTrue(self.send()["ok"])

    def check_redaction(self, channel=None):
        value = "ephemeral fixture text"
        reply = self.send(channel=channel, action="type", input_text=value)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["value_len"], len(value))
        self.assertEqual(reply["value_sha256"], sha256(value.encode()).hexdigest())
        self.assertNotIn(value, json.dumps(reply))
        for path in self.root.iterdir():
            self.assertNotIn(value.encode(), path.read_bytes())

    def test_artifacts_keep_only_value_metadata(self):
        self.check_redaction()

    def test_duplicate_and_conflict_across_store_reopen(self):
        original = self.request()
        reply = self.send(original)
        replica = self.build(store=RequestStore(self.path))
        self.assertEqual(self.send(original, channel=replica), reply)
        self.assertEqual(self.send(dict(original, action="focus"), channel=replica)["code"], "REQUEST_ID_CONFLICT")
        self.performed["cdp"].assert_called_once()

    def test_concurrent_duplicate_is_never_executed_twice(self):
        entered, resume = Event(), Event()

        def blocking_action(lock_id):
            entered.set()
            if not resume.wait(3):
                raise TimeoutError
            return self.perform_action(lock_id)

        self.performed["cdp"].side_effect = blocking_action
        other = self.build(store=RequestStore(self.path))
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.send)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.send(channel=other)["code"], "UNCERTAIN_OUTCOME")
            finally:
                resume.set()
            self.assertTrue(first.result(timeout=3)["ok"])
        self.performed["cdp"].assert_called_once()

    def test_pending_reservation_survives_executor_loss(self):
        self.assertTrue(self.store.reserve(AGENT, self.request())[0])
        other = self.build(store=RequestStore(self.path))
        self.assertEqual(self.send(channel=other)["code"], "UNCERTAIN_OUTCOME")
        self.assert_no_backend()

    def test_dispatch_requires_a_broker_lock(self):
        with patch.object(self.broker, "try_acquire", return_value=None):
            self.assertEqual(self.send()["code"], "NO_LEASE")
        self.assert_no_backend()

    def test_dispatch_requires_correlated_broker_acknowledgement(self):
        actual_dispatch = self.broker.dispatch

        def wrong_mark(agent_id, lock_id):
            return dict(actual_dispatch(agent_id, lock_id), event_id="different-request")

        with patch.object(self.broker, "dispatch", side_effect=wrong_mark):
            self.assertEqual(self.send()["code"], "UNCERTAIN_OUTCOME")
        self.performed["cdp"].assert_not_called()
        self.assertEqual(self.broker.lock_status(SURFACE)["status"], "UNKNOWN")

    def test_success_marks_send_and_keeps_verified_readback(self):
        reply = self.send()
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["ax_sha256"], POST_AX)
        self.performed["cdp"].assert_called_once()
        self.assertIsNone(self.broker.lock_status(SURFACE))

    def test_qualification_requires_wl0_and_scoped_host_action(self):
        self.wl0["apps"]["google-chrome"]["cdp"]["status"] = "NOT RUN"
        self.assertEqual(self.send(channel=self.build())["code"], "DISPLAY_UNSUPPORTED")
        self.observation = replace(self.observation, backend="atspi")
        self.assertEqual(self.send(request_id="unqualified", backend="atspi")["code"], "DISPLAY_UNSUPPORTED")
        self.assert_no_backend()
        proof = AtspiAction(NODE, "hidden Google Gemini page tab", "org.a11y.atspi.Action", "DoAction", 0)
        self.observation = replace(self.observation, atspi_action=proof)
        self.assertTrue(self.send(request_id="qualified", backend="atspi")["ok"])
        self.performed["atspi"].assert_called_once()

    def test_recheck_refuses_changed_observation_during_preparation(self):
        def prepare(*_):
            self.observation = replace(self.observation, observation_id="replacement")
            return self.performed["cdp"]

        self.backends["cdp"].side_effect = prepare
        self.assertEqual(self.send()["code"], "STALE_FRAME")
        self.performed["cdp"].assert_not_called()
        self.assertIsNone(self.broker.lock_status(SURFACE))

    def test_stale_ax_projection_is_refused(self):
        self.assertEqual(self.send(target={"ax_stable_sha256": POST_AX})["code"], "STALE_FRAME")
        self.assert_no_backend()

    def test_stale_vm_frame_is_never_admitted(self):
        self.target = replace(self.target, surface_kind="vm")
        stale_target = dict(self.request()["target"], surface_kind="vm", based_on_frame={"sha256": "b" * 64})
        self.assertEqual(self.send(target=stale_target)["code"], "-32602")
        self.assertEqual(self.send()["code"], "DISPLAY_UNSUPPORTED")
        self.assert_no_backend()

    def check_high_risk_fallback_refused(self, channel=None):
        self.use_private_fallback()
        self.assertEqual(self.send(channel=channel, risk_class="enviar")["code"], "FALLBACK_FORBIDDEN_IRREVERSIBLE")
        self.backends["vision"].assert_not_called()

    def test_enviar_without_rung_two_refused(self):
        self.check_high_risk_fallback_refused()

    def test_trusted_risk_floor_overrides_declared_risk_and_approval(self):
        self.risk_for.return_value = "enviar"
        self.authorize.return_value = "HUMAN_NEEDED"
        self.assertEqual(self.send()["code"], "HUMAN_NEEDED")
        self.assertEqual(self.authorize.call_args.args[2], "enviar")
        self.performed["cdp"].assert_not_called()

    def test_identity_mismatch_never_falls_back(self):
        self.use_private_fallback("identity_mismatch")
        self.assertEqual(self.send()["code"], "identity_mismatch")
        self.backends["vision"].assert_not_called()

    def check_fallback_attributed(self, channel=None):
        self.use_private_fallback()
        reply = self.send(channel=channel)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply.get("fallback_from"), "cdp")
        self.performed["cdp"].assert_not_called()
        self.performed["vision"].assert_called_once()

    def test_qualified_pre_send_fallback_is_attributed(self):
        self.check_fallback_attributed()

    def test_fallback_respects_the_injectors_risk_floor(self):
        self.use_private_fallback()
        self.risk_for.side_effect = lambda request, _: "enviar" if request["backend"] == "vision" else "leer"
        self.assertEqual(self.send()["code"], "FALLBACK_FORBIDDEN_IRREVERSIBLE")
        self.performed["vision"].assert_not_called()

    def check_timeout_never_retries(self, channel=None):
        self.use_private_fallback(code="UNCERTAIN_OUTCOME")
        self.backends["cdp"].side_effect = None
        self.performed["cdp"].side_effect = TimeoutError
        self.assertEqual(self.send(channel=channel)["code"], "UNCERTAIN_OUTCOME")
        self.assertEqual(self.send(channel=channel)["code"], "UNCERTAIN_OUTCOME")
        self.performed["cdp"].assert_called_once()
        self.backends["vision"].assert_not_called()
        self.assertEqual(self.broker.lock_status(SURFACE)["status"], "UNKNOWN")
        self.assertEqual(self.send(request_id="new-injector", backend="vision")["code"], "NO_LEASE")
        self.performed["vision"].assert_not_called()

    def test_post_send_timeout_quarantines_with_no_retry_or_injector_change(self):
        self.check_timeout_never_retries()

    def test_transport_ack_without_readback_is_uncertain(self):
        self.performed["cdp"].side_effect = None
        self.performed["cdp"].return_value = {"schema": "vp.channel.reply/v1", "ok": True, "code": "OK"}
        self.assertEqual(self.send()["code"], "UNCERTAIN_OUTCOME")
        self.assertEqual(self.broker.lock_status(SURFACE)["status"], "UNKNOWN")

    def test_observe_can_collect_reconciliation_evidence_without_releasing_custody(self):
        self.performed["cdp"].side_effect = TimeoutError
        self.assertEqual(self.send()["code"], "UNCERTAIN_OUTCOME")
        self.performed["cdp"].side_effect = None
        self.performed["cdp"].return_value = {"schema": "vp.channel.reply/v1", "ok": True,
                                                "code": "OBSERVED", "ax_sha256": POST_AX}
        observation = dict(example("vp.channel.req"), request_id="reconcile-observe", dry_run=False)
        self.assertEqual(self.send(observation)["ax_sha256"], POST_AX)
        self.performed["cdp"].assert_called_with(None)
        self.assertEqual(self.broker.lock_status(SURFACE)["status"], "UNKNOWN")

    def test_mutant_dropped_epoch_check(self):
        bad = variant(core, "ChannelCore",
                      ('if not request["authority_epoch"] == lease["authority_epoch"] == session["lease"]["authority_epoch"]:',
                       'if False:'),
                      ('if (binding["owner"], binding["authority_epoch"]) != (agent_id, request["authority_epoch"]):',
                       'if binding["owner"] != agent_id:'))
        with self.assertRaises(AssertionError):
            self.check_epoch_refused(self.build(bad))

    def test_mutant_persisted_input_text(self):
        bad = variant(core.request_store, "RequestStore",
                      ('sha256(canonical_bytes([agent_id, request])).hexdigest()',
                       'canonical_bytes([agent_id, request]).decode()'))
        with self.assertRaises(AssertionError):
            self.check_redaction(self.build(store=bad(self.path)))

    def test_mutant_fallback_at_enviar(self):
        bad = variant(core, "ChannelCore",
                      ('high_risk = act and RISKS.index(risk) >= RISKS.index("enviar")', 'high_risk = False'))
        with self.assertRaises(AssertionError):
            self.check_high_risk_fallback_refused(self.build(bad))

    def test_mutant_missing_fallback_from(self):
        bad = variant(core, "ChannelCore",
                      ('reply["fallback_from"] = fallback_from\n            return reply', 'pass\n            return reply'))
        with self.assertRaises(AssertionError):
            self.check_fallback_attributed(self.build(bad))

    def test_mutant_retry_after_send_timeout(self):
        bad = variant(core, "ChannelCore", ('reply = self._verified_reply(perform(lock_id), routed)',
                      'try:\n                reply = self._verified_reply(perform(lock_id), routed)\n'
                      '            except TimeoutError:\n                reply = self._verified_reply(perform(lock_id), routed)'))
        with self.assertRaises(AssertionError):
            self.check_timeout_never_retries(self.build(bad))
