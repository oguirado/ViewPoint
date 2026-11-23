"""C1 acceptance, refusals and negative controls using frozen wire fixtures."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from jsonschema import ValidationError


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT.parents[1] / "schemas" / "tests" / "examples"
SPEC = importlib.util.spec_from_file_location("vp_control", ROOT / "control.py")
c = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = c
SPEC.loader.exec_module(c)


def fixture(name):
    return json.loads((EXAMPLES / name).read_bytes())


ADAPTER_PAIRS = fixture("adapter_pairs.json")
AGENT = c.LaneIdentity("agent", "fixture-agent")
OPERATOR = c.LaneIdentity("operator", "fixture-operator")
WINDOW = "vp_window_summon"


def request(tool=WINDOW, args=None, *, request_id="r-fixture", dry_run=True):
    return {
        "schema": c.REQUEST, "op": tool, "request_id": request_id, "dry_run": dry_run,
        "context": {"caller": AGENT.caller, "agent_instance": AGENT.agent_instance},
        "args": {"session_id": "fixture-session"} if args is None else args,
    }


def allow(_request, _risk):
    return True


def result(_request):
    return c.Outcome(True, "OK")


class ControlTests(unittest.TestCase):
    def setUp(self):
        workspace = self.enterContext(TemporaryDirectory(dir=ROOT / "tests", prefix=".control-test-"))
        self.directory = Path(workspace)
        self.window = fixture("vp.capability_v1.valid.json")
        self.registry = c.CapabilityRegistry([self.window])

    def dispatcher(self, *, db="requests.sqlite", registry=None, pace=allow, lease=allow,
                   tool_handler=None, bindings=()):
        return c.Dispatcher(
            registry or self.registry, c.RequestStore(self.directory / db), pace_risk=pace,
            lease_epoch=lease, tools={WINDOW: tool_handler or c.Handler(result, result)},
            bindings=bindings,
        )

    def parity(self, *, mcp_params=None, cli_request=None, dispatcher_type=None):
        envelope = request()
        context = {"session_id": "fixture-session", "authority_epoch": 4}
        envelope["context"].update(context)
        flat = {"request_id": envelope["request_id"], "dry_run": True,
                "session_id": "fixture-session", "authority_epoch": 4}
        make_dispatcher = dispatcher_type or self.dispatcher
        replies = (
            make_dispatcher(db="mcp.sqlite").dispatch_mcp(
                WINDOW, flat if mcp_params is None else mcp_params, AGENT,
                binding_context={"session_id": "fixture-session"},
            ),
            make_dispatcher(db="cli.sqlite").dispatch_cli(
                "window-summon", envelope if cli_request is None else cli_request, AGENT,
            ),
            make_dispatcher(db="mailbox.sqlite").dispatch_mailbox(envelope, AGENT),
        )
        for reply in replies:
            self.assertEqual(reply["code"], "OK")
            self.assertEqual(reply["effective_request_sha256"], c.digest(envelope))
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(replies[1], replies[2])

    def test_mcp_cli_mailbox_digest_parity(self):
        self.parity()

    def test_mutant_mcp_request_wrapper_is_killed(self):
        with self.assertRaises(c.InvalidRequest) as error:
            self.parity(mcp_params={"request": request()})
        self.assertEqual(error.exception.code, -32602)

    def test_mutant_context_flattened_into_args_is_killed(self):
        broken = request()
        broken["args"].update(broken["context"], authority_epoch=4)
        with self.assertRaises(c.InvalidRequest) as error:
            self.parity(cli_request=broken)
        self.assertEqual(error.exception.code, -32602)

    def test_mutant_lane_specific_default_is_killed(self):
        def dispatcher_with_cli_default(*, db):
            dispatcher = self.dispatcher(db=db)
            original = dispatcher.dispatch_cli

            def dispatch_cli(verb, envelope, identity):
                return original(verb, dict(envelope, dry_run=False), identity)

            dispatcher.dispatch_cli = dispatch_cli
            return dispatcher

        with self.assertRaisesRegex(AssertionError, "[a-f0-9]{64}"):
            self.parity(dispatcher_type=dispatcher_with_cli_default)

    def test_all_lanes_require_explicit_dry_run(self):
        dispatcher = self.dispatcher()
        envelope = request()
        del envelope["dry_run"]
        calls = (
            lambda: dispatcher.dispatch_mcp(WINDOW, {"request_id": "r", "session_id": "s"}, AGENT),
            lambda: dispatcher.dispatch_cli("window-summon", envelope, AGENT),
            lambda: dispatcher.dispatch_mailbox(envelope, AGENT),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaises(c.InvalidRequest) as error:
                call()
            self.assertEqual(error.exception.code, -32602)

    def test_unknown_and_unregistered_ops_are_refused(self):
        dispatcher = self.dispatcher()
        for tool in ("unknown", "vp_session_list"):
            with self.subTest(tool=tool), self.assertRaises(c.InvalidRequest) as error:
                dispatcher.dispatch_mailbox(request(tool, {}), AGENT)
            self.assertEqual(error.exception.code, -32601)

    def test_operator_only_refuses_agent_even_with_spoofed_context(self):
        tool = "vp_identity_delete"
        identity_delete = dict(self.window, tool=tool, json_op=tool, cli="identity", operator_only=True)
        registry = c.CapabilityRegistry([identity_delete])
        dispatcher = self.dispatcher(registry=registry)
        envelope = request(tool, {})
        envelope["context"].update(caller="operator", agent_instance="spoof")
        stamped = deepcopy(envelope)
        stamped["context"] = {"caller": AGENT.caller, "agent_instance": AGENT.agent_instance}
        reply = dispatcher.dispatch_cli("identity", envelope, AGENT)
        self.assertEqual(reply["code"], "OWNER")
        self.assertEqual(reply["effective_request_sha256"], c.digest(stamped))
        self.assertIsNone(dispatcher.requests.claim(envelope["request_id"], c.digest(stamped)))
        with self.assertRaises(c.InvalidRequest):
            dispatcher.dispatch_mcp(tool, {"caller": "operator"}, AGENT)
        with self.assertRaisesRegex(ValueError, "operator-only"):
            c.CapabilityRegistry([dict(identity_delete, operator_only=False)])

    def test_conflicting_target_context_is_refused(self):
        envelope = request()
        envelope["context"]["session_id"] = "different-session"
        with self.assertRaises(c.InvalidRequest) as error:
            self.dispatcher().dispatch_mailbox(envelope, AGENT)
        self.assertEqual(error.exception.code, -32602)

    def test_request_id_conflict_and_exact_retry(self):
        calls = []

        def preview(normalized):
            calls.append(normalized)
            return result(normalized)

        handler = c.Handler(result, preview)
        first = self.dispatcher(tool_handler=handler)
        other_lane = self.dispatcher(tool_handler=handler)
        envelope = request()
        original = first.dispatch_mailbox(envelope, AGENT)
        self.assertEqual(other_lane.dispatch_cli("window-summon", envelope, AGENT), original)
        changed = dict(envelope, args={"session_id": "different-session"})
        conflict = other_lane.dispatch_mailbox(changed, AGENT)
        self.assertEqual(conflict["code"], "REQUEST_ID_CONFLICT")
        self.assertEqual(conflict["effective_request_sha256"], c.digest(changed))
        self.assertEqual(calls, [envelope])

    def test_inflight_duplicate_never_enters_backend(self):
        entered, release = Event(), Event()

        def execute(normalized):
            entered.set()
            if not release.wait(10):
                raise TimeoutError
            return result(normalized)

        handler = c.Handler(execute)
        first = self.dispatcher(tool_handler=handler)
        other_lane = self.dispatcher(tool_handler=handler)
        envelope = request(dry_run=False)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(first.dispatch_mailbox, envelope, AGENT)
            try:
                self.assertTrue(entered.wait(10))
                duplicate = other_lane.dispatch_mailbox(envelope, AGENT)
                self.assertEqual(duplicate["code"], "UNCERTAIN_OUTCOME")
            finally:
                release.set()
            self.assertTrue(pending.result()["ok"])

    def test_gates_run_in_order_and_replay_rechecks_lease(self):
        calls = []
        lease = True

        def pace_gate(_request, risk):
            calls.append(("pace", risk))
            return True

        def lease_gate(_request, risk):
            calls.append(("lease", risk))
            return lease

        def execute(normalized):
            calls.append(("execute", normalized["dry_run"]))
            return result(normalized)

        dispatcher = self.dispatcher(pace=pace_gate, lease=lease_gate, tool_handler=c.Handler(execute))
        envelope = request(dry_run=False)
        self.assertEqual(dispatcher.dispatch_mailbox(envelope, AGENT)["code"], "OK")
        self.assertEqual(calls, [("pace", self.window["risk_class"]),
                                 ("lease", self.window["risk_class"]), ("execute", False)])
        lease = None
        self.assertEqual(dispatcher.dispatch_mailbox(envelope, AGENT)["code"], "NO_LEASE")
        self.assertEqual(calls[-1], ("lease", self.window["risk_class"]))
        self.assertEqual(calls.count(("execute", False)), 1)

    def test_dry_run_requires_a_preview_backend(self):
        calls = []
        handler = c.Handler(lambda req: calls.append(req) or result(req))
        self.assertEqual(self.dispatcher(tool_handler=handler).dispatch_mailbox(request(), AGENT)["code"], "HOLD")
        self.assertEqual(calls, [])

    def test_pace_refusal_prevents_lease_check_and_backend_dispatch(self):
        def unexpected(*_args):
            self.fail("Pace refusal must stop dispatch")

        dispatcher = self.dispatcher(pace=lambda *_: "PACE_LIMITED", lease=unexpected,
                                     tool_handler=c.Handler(unexpected, unexpected))
        self.assertEqual(dispatcher.dispatch_mailbox(request(), AGENT)["code"], "PACE_LIMITED")

    def test_four_new_adapters_are_listed_with_frozen_policy(self):
        for tool, cli in (("vp_perception_call", "perception"), ("vp_guest_call", "guest"),
                          ("vp_data_call", "data"), ("vp_factory_call", "factory")):
            with self.subTest(tool=tool):
                entry = self.registry.get(tool)
                self.assertEqual(entry["json_op"], tool)
                self.assertEqual(entry["cli"], cli)
                self.assertEqual(entry["ops"], list(ADAPTER_PAIRS[tool]))
                self.assertFalse(entry["supported"])
                self.assertTrue(all(row["binding"]["kind"] == "none" for row in entry["op_table"]))
        factory = self.registry.get("vp_factory_call")
        publish = next(row for row in factory["op_table"] if row["op"] == "af.publish")
        self.assertTrue(publish["operator_only"])
        self.assertFalse(publish["supported"])

    def test_unbound_ops_and_parked_operator_ops_do_not_dispatch(self):
        dispatcher = self.dispatcher()
        tool = "vp_factory_call"
        generate = request(tool, ADAPTER_PAIRS[tool]["af.generate"]["args"], request_id="generate")
        self.assertEqual(dispatcher.dispatch_mailbox(generate, AGENT)["code"], "HOLD")
        publish = request(tool, ADAPTER_PAIRS[tool]["af.publish"]["args"])
        self.assertEqual(dispatcher.dispatch_mailbox(publish, AGENT)["code"], "OWNER")
        self.assertEqual(dispatcher.dispatch_mailbox(publish, OPERATOR)["code"], "HOLD")

    def test_factory_binding_alone_does_not_admit_an_operation(self):
        tool = "vp_factory_call"
        catalog = deepcopy(c._validation._adapter_rows())
        catalog[tool]["ops"]["af.generate"].update(
            supported=True, binding={"kind": "factory_cmd", "name": "fixture-generate",
                                     "source": self.window["source"][0]},
        )
        with patch.object(c._validation, "_adapter_rows", return_value=catalog):
            registry = c.CapabilityRegistry()
        entry = registry.get(tool)
        self.assertFalse(entry["supported"])
        self.assertFalse(next(row for row in entry["op_table"] if row["op"] == "af.generate")["supported"])

    def test_terminal_args_is_a_flat_parameter_when_frozen(self):
        tool = "vp_terminal_call"
        params = dict(ADAPTER_PAIRS[tool]["screen_open"]["args"], args=["fixture"],
                      request_id="screen-open", dry_run=True)
        reply = self.dispatcher().dispatch_mcp(tool, params, AGENT)
        self.assertEqual(reply["code"], "HOLD")

    def test_missing_frozen_pair_returns_args_schema_absent(self):
        tool = "vp_data_call"
        catalog = deepcopy(c._validation._adapter_rows())
        del catalog[tool]["ops"]["resolve"]["reply_schema"]
        with patch.object(c._validation, "_adapter_rows", return_value=catalog):
            registry = c.CapabilityRegistry()
        reply = self.dispatcher(registry=registry).dispatch_mailbox(
            request(tool, ADAPTER_PAIRS[tool]["resolve"]["args"]), AGENT,
        )
        self.assertEqual(reply["code"], "ARGS_SCHEMA_ABSENT")

    def test_unknown_adapter_selector_has_no_frozen_pair(self):
        with self.assertRaises(c.InvalidRequest) as error:
            self.dispatcher().dispatch_mailbox(request("vp_data_call", {"op": "fixture-unknown"}), AGENT)
        self.assertEqual(error.exception.code, "ARGS_SCHEMA_ABSENT")

    def test_bound_operation_checks_per_op_policy_and_exact_reply(self):
        tool = "vp_capsule_call"
        case = ADAPTER_PAIRS[tool]["stop"]
        other = ADAPTER_PAIRS[tool]["add"]["result"]
        catalog = deepcopy(c._validation._adapter_rows())
        row = catalog[tool]["ops"]["stop"]
        row.update(supported=True, binding={"kind": "core_route", "name": "fixture-stop",
                                           "source": self.window["source"][0]})
        with patch.object(c._validation, "_adapter_rows", return_value=catalog):
            registry = c.CapabilityRegistry()
        binding_key = ("core_route", "fixture-stop")
        observed_risk = []
        dispatcher = self.dispatcher(
            registry=registry, pace=lambda _req, risk: observed_risk.append(risk) or True,
            bindings={binding_key: c.Handler(result, lambda _: c.Outcome(True, "OK", case["result"]))},
        )
        envelope = request(tool, case["args"])
        reply = dispatcher.dispatch_mailbox(envelope, AGENT)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["result"], case["result"])
        self.assertEqual(observed_risk, [row["risk_class"]])
        wrong = self.dispatcher(
            db="wrong.sqlite", registry=registry,
            bindings={binding_key: c.Handler(result, lambda _: c.Outcome(True, "OK", other))},
        )
        self.assertEqual(wrong.dispatch_mailbox(envelope, AGENT)["code"], "UNCERTAIN_OUTCOME")

    def test_registry_preserves_provider_and_rejects_unqualified_support(self):
        pin = self.window["source"][0]["sha256"]
        provider = {"kind": "local_model", "model": {
            "name": "fixture-model", "role": "fixture", "deps": [], "device": "gpu",
            "weights": {"format": "safetensors", "bytes_on_disk": 1, "tree_sha256": pin,
                        "locator": "af://fixture/model"},
            "license": {"id": "fixture", "territory_ok": True},
            "qualification": {"state": "unqualified", "reason": "fixture", "receipt": None},
            "measured": [],
            "residency": {"mode": "cold_process", "lease": "none", "resident_bytes": None},
        }}
        entry = dict(self.window, provider=provider, supported=False)
        registry = c.CapabilityRegistry([entry])
        self.assertEqual(registry.get(WINDOW)["provider"], provider)
        with self.assertRaises(ValidationError):
            c.CapabilityRegistry([dict(entry, supported=True)])

    def test_registry_rejects_a_capability_that_weakens_per_op_risk(self):
        entry = self.registry.get("vp_capsule_call")
        row = next(row for row in entry["op_table"] if row["op"] == "stop")
        row["risk_class"] = "observar"
        with self.assertRaisesRegex(ValueError, "weakens frozen policy"):
            c.CapabilityRegistry([entry])


if __name__ == "__main__":
    unittest.main()
