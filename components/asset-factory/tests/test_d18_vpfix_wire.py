"""Verify the VP-FIX manifest and mailbox exchange against the owner contracts."""

from copy import deepcopy
from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from viewpoint import handlers


FACTORY = Path(__file__).resolve().parents[1]
VIEWPOINT = FACTORY / "viewpoint"
_IN_REPO = FACTORY.parents[1]
OWNER = Path(os.environ.get("VPFIX_OWNER_TREE") or (_IN_REPO if (_IN_REPO / "components/vp-direct/schemas").is_dir() else "/home/admin/Documentos/publish/vp-direct-comms"))
OWNER_COMPONENT = OWNER / "components/vp-direct"
OWNER_SCHEMAS = OWNER_COMPONENT / "schemas"
TOOL = "vp_factory_call"
GAPS = {"af.select_frames", "af.project_views", "af.face_fit", "af.mux"}
REPLAY_NOTE = "The local CLI keeps no replay ledger. The C4 mailbox ledger is authoritative."


def read_json(path):
    return json.loads(path.read_bytes())


def manifest_entry():
    return read_json(VIEWPOINT / "capabilities.json")["capabilities"][0]


def request(op="af.generate", request_id="rp1-test"):
    return {
        "schema": "vp.control.req/v1",
        "request_id": request_id,
        "op": TOOL,
        "context": {"caller": "agent", "agent_instance": "rp1-test", "session_id": "session-1"},
        "args": {"op": op, "goal_id": "goal-1", "inputs": []},
        "dry_run": True,
    }


def reference(stage="matte", digest="a" * 64):
    return {
        "schema": "vp.artifact_ref/v1",
        "locator": "af://fixtures/" + stage + "@sha256:" + digest,
        "sha256": digest,
        "bytes": 17,
        "media_type": "application/octet-stream",
        "stage": stage,
        "copy": "none",
    }


class OwnerWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("vpfix_owner_validation", OWNER_SCHEMAS / "validation.py")
        cls.owner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.owner)

    def test_single_capability_and_frozen_pins(self):
        manifest = read_json(VIEWPOINT / "capabilities.json")
        self.assertEqual(manifest["schema"], "vp.capability_manifest/v1")
        self.assertEqual(manifest["origin"], "factory")
        self.assertEqual(len(manifest["capabilities"]), 1)
        cap, = manifest["capabilities"]
        self.assertEqual((cap["tool"], cap["json_op"], cap["origin"]), (TOOL, TOOL, "factory"))
        self.owner.validate_contract("vp.capability/v1", cap)
        args = OWNER_SCHEMAS / "vp_factory_call.args_v1.schema.json"
        self.assertEqual(cap["args_schema_sha256"], sha256(args.read_bytes()).hexdigest())
        args_document = read_json(args)
        catalog = read_json(OWNER_SCHEMAS / "adapter_ops.json")[TOOL]["ops"]
        self.assertEqual(set(cap["ops"]), set(args_document["properties"]["op"]["enum"]))
        self.assertEqual(cap["ops"], [row["op"] for row in cap["op_table"]])
        for row in cap["op_table"]:
            pinned = catalog[row["op"]]
            for field in ("risk_class", "operator_only", "args_schema_sha256", "reply_schema_sha256"):
                self.assertEqual(row[field], pinned[field], (row["op"], field))
            for field in ("args_schema", "reply_schema"):
                name = pinned[field].removeprefix(TOOL + ".").removesuffix("/v1")
                resource = args_document["$defs"][name]
                self.assertEqual(row[field + "_sha256"], sha256(self.owner.canonical_bytes(resource)).hexdigest())
            self.assertFalse(row["supported"], row["op"])
            self.assertEqual(row["binding"]["kind"], "none")
        for pin in cap["source"]:
            content = (FACTORY.parents[1] / pin["path"]).read_bytes()
            self.assertEqual(pin["sha256"], sha256(content).hexdigest())
            self.assertLessEqual(1, pin["line_start"])
            self.assertLessEqual(pin["line_start"], pin["line_end"])
            self.assertLessEqual(pin["line_end"], len(content.splitlines()))
        self.assertIn(REPLAY_NOTE, manifest["notes"])

    def test_owner_rejects_gap_even_when_unsupported(self):
        cap = manifest_entry()
        gap_rows = read_json(VIEWPOINT / "fixtures/owner-gaps.json")
        self.assertEqual({row["op"] for row in gap_rows}, GAPS)
        for gap in gap_rows:
            self.assertIs(gap["supported"], False)
            offline = deepcopy(cap)
            offline["ops"].append(gap["op"])
            offline["op_table"].append(dict(offline["op_table"][0], op=gap["op"], supported=False))
            with self.assertRaises(self.owner.ValidationError):
                self.owner.validate_contract("vp.capability/v1", offline)
            with self.assertRaises(self.owner.ValidationError):
                self.owner.validate_contract("vp.control.req/v1", request(gap["op"]))
        for field, value in (("risk_class", "preparar"), ("operator_only", False), ("supported", True)):
            with self.subTest(field=field):
                bad = deepcopy(cap)
                next(row for row in bad["op_table"] if row["op"] == "af.publish")[field] = value
                with self.assertRaises(self.owner.ValidationError):
                    self.owner.validate_contract("vp.capability/v1", bad)

    def test_mailbox_pairs_validate_and_match_dispatch(self):
        fixtures = VIEWPOINT / "fixtures"
        requests = {p.name.removesuffix(".req.json"): p for p in fixtures.glob("*.req.json")}
        replies = {p.name.removesuffix(".reply.json"): p for p in fixtures.glob("*.reply.json")}
        self.assertEqual(requests.keys(), replies.keys())
        seen = set()
        for request_id, path in requests.items():
            req = read_json(path)
            reply = read_json(replies[request_id])
            with self.subTest(op=req["args"]["op"]):
                self.assertEqual(req["op"], TOOL)
                self.assertEqual(req["request_id"], request_id)
                self.assertIs(req["dry_run"], True)
                for kind in ("vp.control.req/v1", "vp.oflane.req/v1"):
                    self.owner.validate_contract(kind, req)
                for kind in ("vp.control.reply/v1", "vp.oflane.reply/v1"):
                    self.owner.validate_contract(kind, reply)
                self.owner.validate_adapter_exchange(req, reply)
                self.assertEqual(reply["effective_request_sha256"],
                                 sha256(self.owner.canonical_bytes(req)).hexdigest())
                self.assertEqual(reply, handlers.LocalDryRun().handle(req, dry_run=True))
                self.assertIs(reply["ok"], False)
                self.assertEqual(reply["evidence"], [])
                self.assertIn(REPLAY_NOTE, reply["not_verified"])
                with self.assertRaises(self.owner.ValidationError):
                    self.owner.validate_contract("vp.oflane.req/v1", dict(req, op=req["args"]["op"]))
                with self.assertRaises(self.owner.ValidationError):
                    self.owner.validate_adapter_exchange(req, dict(reply, request_id="wrong"))
                with self.assertRaises(self.owner.ValidationError):
                    self.owner.validate_contract("vp.oflane.reply/v1", dict(reply, ok=True))
                seen.add(req["args"]["op"])
        self.assertGreaterEqual(seen, {"af.generate", "af.publish"})

    def test_every_frozen_operation_dispatches_without_claiming_live_success(self):
        cap = manifest_entry()
        for row in cap["op_table"]:
            with self.subTest(op=row["op"]):
                req = request(row["op"])
                reply = handlers.LocalDryRun().handle(req)
                self.owner.validate_adapter_exchange(req, reply)
                self.assertEqual(reply["code"], "HOLD")
                self.assertFalse(reply["ok"])
                self.assertIn(
                    f"risk_class={row['risk_class']}, operator_only={str(row['operator_only']).lower()}",
                    reply["not_verified"],
                )


class WireHandlerTests(unittest.TestCase):
    def test_dispatch_refuses_live_and_uses_manifest_policy(self):
        lane = handlers.LocalDryRun()
        req = request("af.publish")
        req["args"]["inputs"] = [reference("export_plan")]
        reply = lane.handle(req, dry_run=True)
        self.assertEqual(reply["op"], TOOL)
        self.assertEqual(lane.handle(req), reply)
        self.assertEqual(reply["code"], "HOLD")
        self.assertIn("risk_class=enviar, operator_only=true", reply["not_verified"])
        self.assertEqual(lane.handle(req, dry_run=False)["code"], "DRY_RUN_REQUIRED")
        self.assertEqual(lane.handle(dict(req, dry_run=False), dry_run=True)["code"], "DRY_RUN_REQUIRED")
        for injection in (dict(req, risk_class="leer"), dict(req, args=dict(req["args"], operator_only=False)),
                          dict(req, context=dict(req["context"], operator_only=False))):
            self.assertIn(lane.handle(injection, dry_run=True)["code"], {"INVALID_REQUEST", "INVALID_ARGS"})
        self.assertEqual(lane.handle(request("af.mux"), dry_run=True)["code"], "ARGS_SCHEMA_ABSENT")

    def test_owner_inputs_permit_seed_and_context_bind_replay(self):
        req = request()
        req["args"].update(inputs=[reference()], permit_ref="permit-1", seed=42)
        lane = handlers.LocalDryRun()
        reply = lane.handle(req, dry_run=True)
        self.assertEqual(reply["code"], "HOLD")
        self.assertEqual(lane.handle(deepcopy(req), dry_run=True), reply)
        for name, value in (("seed", 43), ("permit_ref", "permit-2"), ("inputs", [reference(digest="b" * 64)])):
            changed = dict(req, args=dict(req["args"], **{name: value}))
            self.assertEqual(lane.handle(changed, dry_run=True)["code"], "REQUEST_ID_CONFLICT")
        changed = dict(req, context=dict(req["context"], capsule_id="different"))
        self.assertEqual(lane.handle(changed, dry_run=True)["code"], "REQUEST_ID_CONFLICT")

    def test_owner_input_refusals(self):
        for args in ({"inputs": [{"sha256": "a" * 64}]}, {"inputs": [dict(reference(), locator="af://../paper")]},
                     {"inputs": [dict(reference(), locator=reference()["locator"][:-1] + "b")]},
                     {"seed": True}, {"permit_ref": "../bad"}, {"permit_ref": "bad\n"}):
            req = request()
            req["args"].update(args)
            self.assertEqual(handlers.LocalDryRun().handle(req, dry_run=True)["code"], "INVALID_ARGS")

    def test_limits_apply_before_replay_and_in_process(self):
        lane = handlers.LocalDryRun()
        req = request()
        self.assertEqual(lane.handle(dict(req, request_id="r" * 129), dry_run=True)["code"], "INVALID_REQUEST")
        self.assertEqual(lane.handle(dict(req, args=dict(req["args"], goal_id="g" * 129)), dry_run=True)["code"],
                         "INVALID_ARGS")
        self.assertEqual(lane.handle(dict(req, context=dict(req["context"], session_id="s" * 129)), dry_run=True)["code"],
                         "INVALID_REQUEST")
        large = dict(req, args=dict(req["args"], inputs=[reference()] * 240))
        self.assertEqual(lane.handle(large, dry_run=True)["code"], "INVALID_REQUEST")

        # This request fits the character limit but exceeds the UTF-8 byte limit.
        wide_ref = dict(reference(), media_type="é" * 33000)
        wide = dict(req, args=dict(req["args"], inputs=[wide_ref]))
        raw = json.dumps(wide, ensure_ascii=False)
        self.assertLess(len(raw), 65536)
        self.assertGreater(len(raw.encode("utf-8")), 65536)
        self.assertEqual(lane.handle(wide, dry_run=True)["code"], "INVALID_REQUEST")
        out = io.StringIO()
        self.assertEqual(handlers.main(["--dry-run"], io.StringIO(raw), out), 2)
        self.assertEqual(json.loads(out.getvalue())["code"], "INVALID_REQUEST")
        broken = dict(req, args=dict(req["args"], goal_id="\ud800"))
        self.assertEqual(lane.handle(broken)["code"], "INVALID_REQUEST")
        out = io.StringIO()
        self.assertEqual(handlers.main(["--dry-run"], io.StringIO(json.dumps(broken)), out), 2)
        self.assertEqual(json.loads(out.getvalue())["code"], "INVALID_REQUEST")
        self.assertEqual(lane.handle(req, dry_run=True)["code"], "HOLD")
        legacy = {"request_id": "r", "op": "af.matte", "args": {"goal_id": "g" * 129, "frames_sha256": "a" * 64},
                  "risk_class": "preparar", "operator_only": False}
        self.assertEqual(lane.handle(legacy, dry_run=True)["code"], "INVALID_ARGS")

    def test_cli_discloses_replay_scope_and_local_ledger_stops_at_cap(self):
        req = request()
        for payload in (req, dict(req, args=dict(req["args"], goal_id="different"))):
            output = io.StringIO()
            self.assertEqual(handlers.main(["--dry-run"], io.StringIO(json.dumps(payload)), output), 3)
            self.assertIn(REPLAY_NOTE, json.loads(output.getvalue())["not_verified"])
        with patch.object(handlers, "_MAX_REPLAY_ENTRIES", 1):
            lane = handlers.LocalDryRun()
            old = lane.handle(req, dry_run=True)
            other = lane.handle(dict(req, request_id="another"), dry_run=True)
            self.assertEqual((old["code"], other["code"]), ("HOLD", "BUDGET_EXCEEDED"))
            self.assertEqual(lane.handle(req, dry_run=True), old)
            changed = dict(req, args=dict(req["args"], goal_id="changed"))
            self.assertEqual(lane.handle(changed, dry_run=True)["code"], "REQUEST_ID_CONFLICT")
