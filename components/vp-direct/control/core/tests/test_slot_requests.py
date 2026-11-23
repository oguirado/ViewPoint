# SPDX-License-Identifier: AGPL-3.0-only
"""Entry point tests for the rea.slot.project control module."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from jsonschema import ValidationError


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT.parent

SPEC = importlib.util.spec_from_file_location("vp_slot_requests", BASE / "slot_requests.py")
slot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = slot
SPEC.loader.exec_module(slot)

CTRL_SPEC = importlib.util.spec_from_file_location("vp_control", ROOT / "control.py")
ctrl = importlib.util.module_from_spec(CTRL_SPEC)
sys.modules[CTRL_SPEC.name] = ctrl
CTRL_SPEC.loader.exec_module(ctrl)


KIT_MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "schemas" / "tests" / "fixtures" / "rea_kit" / "examples" / "rea.slot.json"
)


def _kit_manifest():
    return json.loads(KIT_MANIFEST_PATH.read_text(encoding="utf-8"))


def _normalized_request(envelope):
    """Schema-valid ``dry_run.normalized_request`` derived from the kit manifest.

    External routes (``ext.<slot>.<op>``) use the 12-field external form
    from PROJECTIONS.md (manifest ``perception.required_level`` and first
    ``determinism_modes``). Other routes use the 6-field form with
    ``manifest_digest`` null and canonicalised ``arguments``.
    """
    route_id = envelope["args"]["route_id"]
    if route_id.startswith("ext."):
        manifest = _kit_manifest()
        parts = route_id.split(".")
        slot_id = parts[1] if len(parts) >= 3 else manifest["slot_id"]
        op_id = parts[-1] if len(parts) >= 2 else manifest["operations"][0]["id"]
        return {
            "route_id": route_id,
            "manifest_digest": "a" * 64,
            "slot_id": slot_id,
            "op_id": op_id,
            "installation_ref": "ref-1",
            "request_id": envelope["request_id"],
            "inputs": [],
            "params": {},
            "perception_level": manifest["perception"]["required_level"],
            "determinism_mode": manifest["record_replay"]["determinism_modes"][0],
            "dry_run": True,
            "policy_sha256": "b" * 64,
        }
    try:
        canon_args = json.loads(slot.canonical_bytes(envelope["args"]["arguments"]))
    except (ValueError, TypeError):
        canon_args = deepcopy(envelope["args"]["arguments"])
    return {
        "route_id": route_id,
        "request_id": envelope["request_id"],
        "dry_run": True,
        "policy_sha256": "b" * 64,
        "manifest_digest": None,
        "arguments": canon_args,
    }


def ok_dispatch(envelope):
    return {
        "schema": slot.REPLY,
        "request_id": envelope["request_id"],
        "op": slot.CONTROL_OP,
        "ok": True,
        "code": "OK",
        "result": {
            "kind": "dry_run",
            "dry_run": True,
            "normalized_request": _normalized_request(envelope),
            "outside_capsule": True,
        },
        "effective_request_sha256": slot.digest(envelope),
        "evidence": [],
        "not_verified": [],
    }


class SlotRequestsTests(unittest.TestCase):
    def test_build_request_validates(self):
        envelope = slot.build_request("ext.rea.record", {"slot_id": "rea"}, "w2-local-slot-01", True)
        self.assertEqual(envelope["op"], slot.CONTROL_OP)
        self.assertEqual(envelope["schema"], slot.REQUEST)
        self.assertEqual(envelope["args"], {"route_id": "ext.rea.record", "arguments": {"slot_id": "rea"}})
        slot.validate_request(envelope)
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                slot.validate_request(slot.build_request("host.slot.list", {}, "w2-local-slot-02", dry_run))

    def test_handle_forwards_valid_envelope(self):
        envelope = slot.build_request("ext.rea.record", {"slot_id": "rea"}, "w2-local-slot-03", True)
        reply = slot.handle_envelope(envelope, ok_dispatch)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["result"]["kind"], "dry_run")
        self.assertEqual(reply["op"], slot.CONTROL_OP)
        slot.validate_reply(reply)

    def test_handle_extra_args_raises_invalid_request(self):
        calls = []
        def counting(e):
            calls.append(e)
            return ok_dispatch(e)
        envelope = slot.build_request("ext.rea.record", {}, "w2-local-slot-04", True)
        bad = deepcopy(envelope)
        bad["args"]["extra"] = 1
        with self.assertRaises(slot.SlotRequestError) as error:
            slot.handle_envelope(bad, counting)
        self.assertEqual(error.exception.code, -32602)
        self.assertEqual(calls, [])

    def test_handle_malformed_raises_invalid_request(self):
        calls = []
        def counting(e):
            calls.append(e)
            return ok_dispatch(e)
        envelope = slot.build_request("ext.rea.record", {}, "w2-local-slot-05", True)
        for mutate in (
            lambda e: e.update({"schema": "vp.control.req/v2"}),
            lambda e: e.pop("dry_run"),
            lambda e: e.pop("args"),
            lambda e: e.update({"__unknown__": 1}),
            lambda e: e["args"].pop("arguments"),
        ):
            with self.subTest(mutate=mutate):
                bad = deepcopy(envelope)
                mutate(bad)
                with self.assertRaises(slot.SlotRequestError) as error:
                    slot.handle_envelope(bad, counting)
                self.assertEqual(error.exception.code, -32602)
        self.assertEqual(calls, [])

    def test_handle_unsupported_op_raises_method_not_found(self):
        envelope = slot.build_request("ext.rea.record", {}, "w2-local-slot-06", True)
        for op in ("vp_unknown", "vp_rea_call"):
            with self.subTest(op=op):
                bad = deepcopy(envelope)
                bad["op"] = op
                with self.assertRaises(slot.SlotRequestError) as error:
                    slot.handle_envelope(bad, ok_dispatch)
                self.assertEqual(error.exception.code, -32601)
                with self.assertRaises(ValidationError):
                    slot.validate_request(bad)

    def test_handle_missing_identity_raises_invalid_request(self):
        envelope = slot.build_request("ext.rea.record", {}, "w2-local-slot-07", True)
        bad = deepcopy(envelope)
        del bad["request_id"]
        with self.assertRaises(slot.SlotRequestError) as error:
            slot.handle_envelope(bad, ok_dispatch)
        self.assertEqual(error.exception.code, -32602)
        with self.assertRaises(slot.SlotRequestError):
            slot.handle_envelope(None, ok_dispatch)

    def test_build_request_rejects_request_id_in_arguments(self):
        with self.assertRaises(slot.SlotRequestError):
            slot.build_request("ext.rea.record", {"request_id": "x"}, "w2-local-slot-08", True)

    def test_refusal_reply_echoes_identity(self):
        envelope = slot.build_request("ext.rea.record", {}, "w2-local-slot-09", False)
        reply = slot.refusal_reply(envelope, "SCHEMA_INVALID")
        self.assertEqual(reply["request_id"], envelope["request_id"])
        self.assertEqual(reply["op"], slot.CONTROL_OP)
        self.assertEqual(reply["code"], "SCHEMA_INVALID")
        slot.validate_reply(reply)

    def test_dry_run_false_envelope_validates(self):
        envelope = slot.build_request("ext.rea.record", {"slot_id": "rea"}, "w2-local-slot-10", False)
        slot.validate_request(envelope)

    def test_handle_only_rea_op_accepted(self):
        envelope = slot.build_request("ext.rea.record", {}, "w2-local-slot-op-01", True)
        for op in ("mv_list", "vp_window_summon", "vp_unknown"):
            with self.subTest(op=op):
                bad = deepcopy(envelope)
                bad["op"] = op
                with self.assertRaises(slot.SlotRequestError) as error:
                    slot.handle_envelope(bad, ok_dispatch)
                self.assertEqual(error.exception.code, -32601)

    def test_handle_null_or_missing_arguments_raises_invalid_request(self):
        calls = []
        def counting(e):
            calls.append(e)
            return ok_dispatch(e)
        envelope = slot.build_request("ext.rea.record", {"slot_id": "rea"}, "w2-local-slot-args-01", True)
        for mutate in (
            lambda e: e["args"].update(arguments=None),
            lambda e: e["args"].update(arguments=123),
            lambda e: e["args"].pop("arguments"),
            lambda e: e["args"]["arguments"].update(request_id="x"),
            lambda e: e["args"]["arguments"].update(dry_run=True),
        ):
            with self.subTest(mutate=mutate):
                bad = deepcopy(envelope)
                mutate(bad)
                with self.assertRaises(slot.SlotRequestError) as error:
                    slot.handle_envelope(bad, counting)
                self.assertEqual(error.exception.code, -32602)
        self.assertEqual(calls, [])

    def test_handle_reply_identity_mismatch_rejected(self):
        envelope = slot.build_request("ext.rea.record", {}, "w2-local-slot-id-01", True)
        def wrong_id(env):
            reply = ok_dispatch(env)
            return dict(reply, request_id="WRONG-ID")
        with self.assertRaises(ValidationError):
            slot.handle_envelope(envelope, wrong_id)
        def wrong_op(env):
            reply = ok_dispatch(env)
            return dict(reply, op="mv_list")
        with self.assertRaises(ValidationError):
            slot.handle_envelope(envelope, wrong_op)
        def wrong_digest(env):
            reply = ok_dispatch(env)
            return dict(reply, effective_request_sha256="0" * 64)
        with self.assertRaises(ValidationError):
            slot.handle_envelope(envelope, wrong_digest)


    def test_refusal_result_is_closed_empty_object(self):
        envelope = slot.build_request("ext.rea.record", {}, "w2-local-slot-ref-01", True)
        for code in ("SCHEMA_INVALID", "HOLD", "NO_LEASE", "REQUEST_ID_CONFLICT"):
            with self.subTest(code=code):
                reply = slot.refusal_reply(envelope, code)
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["code"], code)
                self.assertEqual(reply["result"], {})
                self.assertIsNotNone(reply["result"])
                self.assertEqual(reply["effective_request_sha256"], slot.digest(envelope))
                slot.validate_reply(reply)
        # Pre-acceptance failures no longer share the wire shape: they raise.
        bad = deepcopy(envelope)
        bad["args"]["extra"] = 1
        with self.assertRaises(slot.SlotRequestError) as error:
            slot.handle_envelope(bad, ok_dispatch)
        self.assertEqual(error.exception.code, -32602)

    def test_accepted_request_refusal_digest_matches(self):
        envelope = slot.build_request("ext.rea.record", {"slot_id": "rea"}, "w2-local-slot-digest-01", True)
        # Direct refusal for an accepted envelope: digest present and matching.
        reply = slot.refusal_reply(envelope, "HOLD")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["code"], "HOLD")
        self.assertEqual(reply["result"], {})
        self.assertEqual(reply["effective_request_sha256"], slot.digest(envelope))
        slot.validate_reply(reply)
        # A dispatch refusal forwarded through handle_envelope must also
        # carry the carrier-established digest.
        def refusing_dispatch(env):
            return slot.refusal_reply(env, "HOLD")

        forwarded = slot.handle_envelope(envelope, refusing_dispatch)
        self.assertFalse(forwarded["ok"])
        self.assertEqual(forwarded["code"], "HOLD")
        self.assertEqual(forwarded["result"], {})
        self.assertEqual(forwarded["effective_request_sha256"], slot.digest(envelope))
        slot.validate_reply(forwarded)

    def test_pre_acceptance_failures_do_not_fabricate_digest(self):
        calls = []
        def counting(env):
            calls.append(env)
            return ok_dispatch(env)

        envelope = slot.build_request("ext.rea.record", {}, "w2-local-slot-pre-01", True)
        # Missing identity: no reply is produced, no digest is fabricated.
        missing = deepcopy(envelope)
        del missing["request_id"]
        with self.assertRaises(slot.SlotRequestError) as error:
            slot.handle_envelope(missing, counting)
        self.assertEqual(error.exception.code, -32602)
        # Unknown op: same lane handling, no refusal reply.
        unknown = deepcopy(envelope)
        unknown["op"] = "vp_unknown"
        with self.assertRaises(slot.SlotRequestError) as error:
            slot.handle_envelope(unknown, counting)
        self.assertEqual(error.exception.code, -32601)
        # Non-dict envelope: lane error, not a refusal with a digest.
        with self.assertRaises(slot.SlotRequestError):
            slot.handle_envelope(None, counting)
        with self.assertRaises(slot.SlotRequestError):
            slot.refusal_reply({"op": slot.CONTROL_OP}, "SCHEMA_INVALID")
        with self.assertRaises(slot.SlotRequestError):
            slot.refusal_reply({}, "SCHEMA_INVALID")
        self.assertEqual(calls, [])

    def test_dispatch_null_or_populated_refusal_rejected(self):
        envelope = slot.build_request("ext.rea.record", {}, "w2-local-slot-null-01", True)
        def null_refusal(env):
            reply = slot.refusal_reply(env, "HOLD")
            return dict(reply, result=None)

        with self.assertRaises(ValidationError):
            slot.handle_envelope(envelope, null_refusal)

        def populated_refusal(env):
            reply = slot.refusal_reply(env, "HOLD")
            return dict(reply, result={"extra": 1})

        with self.assertRaises(ValidationError):
            slot.handle_envelope(envelope, populated_refusal)

        def wrong_digest_refusal(env):
            reply = slot.refusal_reply(env, "NO_LEASE")
            return dict(reply, effective_request_sha256="0" * 64)

        with self.assertRaises(ValidationError):
            slot.handle_envelope(envelope, wrong_digest_refusal)

        def wrong_id_hold(env):
            reply = slot.refusal_reply(env, "HOLD")
            return dict(reply, request_id="WRONG-ID")

        with self.assertRaises(ValidationError):
            slot.handle_envelope(envelope, wrong_id_hold)

    def test_item3_noncanonical_rejected_before_dispatch(self):
        calls = []

        def counting_stub(env):
            calls.append(env)
            return ok_dispatch(slot.build_request("host.slot.list", {}, env["request_id"], True))

        base = slot.build_request("host.slot.list", {}, "w2-local-slot-item3-01", True)
        fixed_reply = ok_dispatch(base)

        def stub(env):
            calls.append(env)
            return deepcopy(fixed_reply)

        for label, bad_value in (
            ("nan", float("nan")),
            ("inf", float("inf")),
            ("neginf", float("-inf")),
            ("object", object()),
            ("bytes", b"secret"),
        ):
            with self.subTest(label=label):
                bad = deepcopy(base)
                bad["args"]["arguments"]["payload"] = bad_value
                with self.assertRaises(slot.SlotRequestError) as error:
                    slot.handle_envelope(bad, stub)
                self.assertEqual(error.exception.code, -32602)
                self.assertEqual(calls, [])
        seen = []

        def capturing(env):
            seen.append(env)
            return ok_dispatch(env)

        env = slot.build_request("host.slot.list", {}, "w2-local-slot-item3-02", True)
        env["args"]["arguments"]["payload"] = (1, "a", [2])
        reply = slot.handle_envelope(env, capturing)
        self.assertTrue(reply["ok"])
        self.assertEqual(len(seen), 1)
        self.assertIsNot(seen[0], env)
        self.assertEqual(seen[0]["args"]["arguments"]["payload"], [1, "a", [2]])
        self.assertIsInstance(seen[0]["args"]["arguments"]["payload"], list)
        self.assertEqual(seen[0], json.loads(slot.canonical_bytes(env)))

    def test_item4_malformed_carrier_uses_lane_error(self):
        identity = ctrl.LaneIdentity(caller="operator", agent_instance="real-host-01")
        base = slot.build_request("ext.rea.record", {}, "w2-local-slot-item4-01", True)
        calls = []

        def counting(env):
            calls.append(env)
            return ok_dispatch(env)

        for mutate in (
            lambda e: e.update({"schema": "vp.control.req/v2"}),
            lambda e: e.pop("dry_run"),
            lambda e: e.update({"__unknown__": 1}),
        ):
            with self.subTest(mutate_line=mutate):
                bad = deepcopy(base)
                mutate(bad)
                with self.assertRaises(ctrl.InvalidRequest) as cerr:
                    ctrl._normalize(bad, identity)
                self.assertEqual(cerr.exception.code, -32602)
                with self.assertRaises(slot.SlotRequestError) as serr:
                    slot.handle_envelope(bad, counting)
                self.assertEqual(serr.exception.code, cerr.exception.code)
        self.assertEqual(calls, [])

    def test_carrier_digest_is_verified_for_success_and_refusals(self):
        identity = ctrl.LaneIdentity(caller="operator", agent_instance="real-host-01")
        request = slot.build_request("ext.rea.record", {}, "w2-local-slot-carrier", True)
        effective = ctrl._normalize(request, identity)
        self.assertNotEqual(slot.digest(request), ctrl.digest(effective))
        codes = ("OK", "HOLD", "HUMAN_NEEDED", "NO_LEASE", "REQUEST_ID_CONFLICT",
                 "UNCERTAIN_OUTCOME", "PERCEPTION_DENIED", "POOL_TIMEOUT")
        for code in codes:
            with self.subTest(code=code):
                def dispatch(envelope):
                    self.assertEqual(envelope, effective)
                    if code == "OK":
                        return ok_dispatch(envelope)
                    return ctrl._reply(envelope, ctrl.digest(envelope), ctrl.Outcome(False, code))

                reply = slot.handle_envelope(request, dispatch, lane_identity=identity)
                self.assertEqual(reply["code"], code)
                self.assertEqual(reply["effective_request_sha256"], ctrl.digest(effective))

                def unstamped(envelope):
                    return dict(dispatch(envelope), effective_request_sha256=slot.digest(request))

                with self.assertRaises(ValidationError):
                    slot.handle_envelope(request, unstamped, lane_identity=identity)

    def test_hold_and_human_needed_reject_forged_or_unrelated_digests(self):
        request = slot.build_request("ext.rea.record", {}, "w2-local-slot-refusal", True)
        unrelated = slot.build_request("ext.rea.record", {}, "w2-local-slot-other", True)
        for code in ("HOLD", "HUMAN_NEEDED"):
            for value in ("0" * 64, slot.digest(unrelated)):
                with self.subTest(code=code, digest=value):
                    def dispatch(envelope):
                        return dict(slot.refusal_reply(envelope, code), effective_request_sha256=value)

                    with self.assertRaises(ValidationError):
                        slot.handle_envelope(request, dispatch)
            good = slot.handle_envelope(request, lambda envelope: slot.refusal_reply(envelope, code))
            self.assertEqual(good["effective_request_sha256"], slot.digest(request))

    def test_refusal_helper_rejects_malformed_carrier_before_hashing(self):
        request = slot.build_request("ext.rea.record", {}, "w2-local-slot-refusal", True)
        for mutate in (
            lambda envelope: envelope.update(schema="vp.control.req/invalid"),
            lambda envelope: envelope["args"].update(injected=True),
            lambda envelope: envelope["args"]["arguments"].update(payload=float("nan")),
        ):
            with self.subTest(mutation=mutate):
                bad = deepcopy(request)
                mutate(bad)
                with patch.object(slot.hashlib, "sha256", wraps=slot.hashlib.sha256) as sha256:
                    with self.assertRaises(slot.SlotRequestError) as error:
                        slot.refusal_reply(bad)
                    self.assertEqual(error.exception.code, -32602)
                    sha256.assert_not_called()
        self.assertEqual(slot.refusal_reply(request)["effective_request_sha256"], slot.digest(request))

    def test_replay_params_are_checked_before_dispatch_and_on_preview(self):
        request = slot.build_request(
            "ext.rea.record", {"params": {"seed": 1, "mode": "exact", "sample_count": 2}},
            "w2-local-slot-preview", True,
        )
        calls = []

        def dispatch(envelope):
            calls.append(envelope)
            reply = ok_dispatch(envelope)
            reply["result"]["normalized_request"]["params"] = deepcopy(envelope["args"]["arguments"]["params"])
            return reply

        bad = deepcopy(request)
        bad["args"]["arguments"]["params"]["budget_frames"] = 4
        with self.assertRaises(slot.SlotRequestError) as error:
            slot.handle_envelope(bad, dispatch)
        self.assertEqual(error.exception.code, -32602)
        self.assertEqual(calls, [])
        self.assertTrue(slot.handle_envelope(request, dispatch)["ok"])

        def bad_preview(envelope):
            reply = dispatch(envelope)
            reply["result"]["normalized_request"]["params"]["strategy"] = "uniform"
            return reply

        with self.assertRaises(ValidationError):
            slot.handle_envelope(request, bad_preview)

        sample = slot.build_request(
            "ext.example.capture", {"params": {"seed": 1, "budget_frames": 4, "strategy": "uniform"}},
            "w2-local-slot-sample", True, operation_role="sample",
        )
        self.assertTrue(slot.handle_envelope(sample, dispatch, operation_role="sample")["ok"])
        with self.assertRaises(slot.SlotRequestError):
            slot.handle_envelope(sample, dispatch)

    def test_compare_requires_verdict_and_non_compare_forbids_it(self):
        exchange = {
            "kind": "slot_exchange", "status": "OK", "rc": 0, "slot_result_ref": None,
            "evidence_ref": None, "receipt_ref": None, "availability": "admitted",
            "produced_by": "test-adapter", "perception_used": "none", "outside_capsule": True,
        }
        verdict = {"result": "mismatch", "comparator_id": "test", "comparator_version": "1", "verdict_ref": None}
        for op, good, bad in (("compare", dict(exchange, verdict=verdict), exchange),
                              ("record", exchange, dict(exchange, verdict=verdict))):
            with self.subTest(operation=op):
                request = slot.build_request(f"ext.rea.{op}", {}, "w2-local-slot-exchange")
                self.assertEqual(slot.handle_envelope(
                    request, lambda envelope: dict(ok_dispatch(envelope), result=good),
                )["result"], good)
                with self.assertRaises(ValidationError):
                    slot.handle_envelope(
                        request, lambda envelope: dict(ok_dispatch(envelope), result=bad),
                    )

    def test_host_inspect_requires_the_projected_payload(self):
        request = slot.build_request("host.slot.inspect", {"slot_id": "rea"}, "w2-local-slot-inspect")
        for invalid in ({}, "not an inspect object", 42):
            def dispatch(envelope):
                result = {"kind": "host_result", "payload": invalid, "outside_capsule": True}
                return dict(ok_dispatch(envelope), result=result)

            with self.subTest(payload=invalid), self.assertRaises(ValidationError):
                slot.handle_envelope(request, dispatch)


if __name__ == "__main__":
    unittest.main()
