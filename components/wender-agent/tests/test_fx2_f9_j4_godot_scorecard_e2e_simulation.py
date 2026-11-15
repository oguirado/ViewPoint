# @module {"id":"tests.test_fx2_f9_j4_godot_scorecard_e2e_simulation","layer":3,"owner":"TESTS","deps":["wa.state.schema"]}
"""@module tests.test_fx2_f9_j4_godot_scorecard_e2e_simulation - contract tests for Godot perception & executor adapters.

Target APIs to be implemented by build task fx2-f9-godot-t2-mailbox-and-uitree-adapter:
- wender.perceive.godot_adapter.node_id_for_control_path(path)
- wender.perceive.godot_adapter.parse_ui_state(token, registries)
- wender.perceive.godot_adapter.validate_uitree(base, sidecar)
- wender.perceive.godot_adapter.read_outbox_batches(data, decode_batches)
- wender.perceive.godot_adapter.write_inbox_batch(records, encode_batch, sink)
- wender.act.godot_executor.GodotExecutor

All new-behaviour test methods carry @unittest.expectedFailure until the build task lands.
Target imports are executed strictly inside test bodies.
"""
import hashlib
import json
import unittest
from wender.state import schema


# @anchor:tests.test_fx2_f9_j4_godot_scorecard_e2e_simulation.make_canonical_godot_base
def make_canonical_godot_base(tick=0, nodes=None, actionable_ids=None):
    """Build a valid source.kind='godot' base state fixture passing validate_state."""
    if nodes is None:
        root_id = hashlib.sha256(b"godot/root").hexdigest()[:16]
        btn_id = hashlib.sha256(b"godot/button_start").hexdigest()[:16]
        nodes = [
            {
                "id": root_id,
                "role": "window",
                "name": "HoldingsMain",
                "depth": 0,
                "ordinal": 0,
                "parent": None,
                "states": {"focused": "true", "tick": str(tick)},
                "actionable": False,
                "box": [0, 0, 1024, 768],
            },
            {
                "id": btn_id,
                "role": "button",
                "name": "ActionControl",
                "depth": 1,
                "ordinal": 0,
                "parent": root_id,
                "states": {"enabled": "true"},
                "actionable": True,
                "box": [10, 10, 100, 30],
            },
        ]
    if actionable_ids is None:
        actionable_ids = [n["id"] for n in nodes if n["actionable"]]

    ax_bytes = schema.dumps(nodes).encode("utf-8")
    ax_hash = hashlib.sha256(ax_bytes).hexdigest()

    base = {
        "schema": "wender.state/v1",
        "source": {
            "kind": "godot",
            "url": f"godot://holdings/scene?tick={tick}",
            "title": "Holdings AI Scorecard",
        },
        "prune": {"name": "godot_prune", "version": 1},
        "ax_sha256": ax_hash,
        "ax_stable_sha256": ax_hash,
        "viewport": {"w": 1024, "h": 768},
        "nodes": nodes,
        "actionable": actionable_ids,
        "counts": {
            "raw": len(nodes),
            "nodes": len(nodes),
            "actionable": len(actionable_ids),
        },
    }
    errs = schema.validate_state(base)
    if errs:
        raise ValueError(f"Generated base state invalid: {errs}")
    return base


# @anchor:tests.test_fx2_f9_j4_godot_scorecard_e2e_simulation.FixtureCodec
class FixtureCodec:
    """Offline fixture codec owning envelope framing, locking, and publication."""

    HEADER = b"FIXTURE_ENVELOPE_V1\n"
    DELIMITER = b"\nCOMPLETED\n"

    def encode_batch(self, records):
        if not isinstance(records, list):
            raise TypeError("records must be a list")
        body = schema.dumps(records).encode("utf-8")
        return self.HEADER + body + self.DELIMITER

    def decode_batches(self, data):
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        if self.DELIMITER not in data:
            return [], bytes(data)

        parts = data.split(self.DELIMITER, 1)
        batch_bytes = parts[0]
        pending = parts[1]

        if not batch_bytes.startswith(self.HEADER):
            raise ValueError("Malformed batch header")

        payload_bytes = batch_bytes[len(self.HEADER):]
        try:
            records = json.loads(payload_bytes.decode("utf-8"))
        except Exception as x:
            raise ValueError(f"Malformed JSON payload: {x}")

        return [records], pending


# @anchor:tests.test_fx2_f9_j4_godot_scorecard_e2e_simulation.FakeGodotEngine
class FakeGodotEngine:
    """Independent deterministic fake engine implementing Holdings scorecard actions."""

    CLOSED_ACTION_SCHEMAS = {
        "new_game": {"seed", "country_id", "difficulty", "opening_mode"},
        "hire": {"person_id", "terms"},
        "buy_asset": {"company_id", "asset_type", "quantity", "terms"},
        "take_loan": {"company_id", "terms"},
        "advance": {"until_tick"},
        "export_chart": {"chart_id"},
        "read_inbox": {"item_id"},
        "save": set(),
    }

    B3_VOCABULARY = ["node", "control", "family", "states", "actionable"]

    def __init__(self, seed=42):
        self.seed = seed
        self.codec = FixtureCodec()
        self.reset()

    def reset(self):
        self.tick = 0
        self.day = 0
        self.game_started = False
        self.hired = []
        self.assets = []
        self.loans = []
        self.charts = []
        self.inbox_read = []
        self.saved = False
        self.command_log = []
        self.state_history = []
        self.current_base = make_canonical_godot_base(tick=0)
        self.current_hash = self.current_base["ax_sha256"]
        self.state_history.append((0, self.current_hash, schema.dumps(self.current_base)))

    def execute_action(self, action_name, args, basis_sha256):
        if basis_sha256 != self.current_hash:
            return {"status": "rejected", "reason": "stale_hash", "committed_tick": self.tick}

        if action_name not in self.CLOSED_ACTION_SCHEMAS:
            return {"status": "rejected", "reason": "unregistered_action", "committed_tick": self.tick}

        expected_keys = self.CLOSED_ACTION_SCHEMAS[action_name]
        if set(args.keys()) != expected_keys:
            return {"status": "rejected", "reason": "schema_mismatch", "committed_tick": self.tick}

        # Apply state transition
        if action_name == "new_game":
            self.game_started = True
            self.tick += 1
        elif action_name == "hire":
            self.hired.append(args)
            self.tick += 1
        elif action_name == "buy_asset":
            self.assets.append(args)
            self.tick += 1
        elif action_name == "take_loan":
            self.loans.append(args)
            self.tick += 1
        elif action_name == "advance":
            until_tick = args["until_tick"]
            if until_tick > self.tick:
                self.tick = until_tick
                self.day = until_tick  # 1 day per tick fixture mapping
        elif action_name == "export_chart":
            self.charts.append(args["chart_id"])
            self.tick += 1
        elif action_name == "read_inbox":
            self.inbox_read.append(args["item_id"])
            self.tick += 1
        elif action_name == "save":
            self.saved = True
            self.tick += 1

        cmd_entry = {"action": action_name, "args": args, "tick": self.tick}
        self.command_log.append(cmd_entry)

        # Generate updated canonical base state
        self.current_base = make_canonical_godot_base(tick=self.tick)
        self.current_hash = self.current_base["ax_sha256"]
        canonical_dumps = schema.dumps(self.current_base)
        self.state_history.append((self.tick, self.current_hash, canonical_dumps))

        sidecar = None
        if action_name == "export_chart":
            sidecar = {
                "ax_sha256": self.current_hash,
                "chart_series": [100, 150, 200, 250],
                "refresh_cadence": 5,
                "committed_source_tick": self.tick,
                "public_action_registry": sorted(list(self.CLOSED_ACTION_SCHEMAS.keys())),
                "b3_vocabulary": self.B3_VOCABULARY,
            }

        outcome_record = {
            "status": "executed",
            "action": action_name,
            "committed_tick": self.tick,
            "committed_sha256": self.current_hash,
            "chart_series": [100, 150, 200, 250] if action_name == "export_chart" else None,
            "refresh_cadence": 5 if action_name == "export_chart" else None,
            "b3_vocabulary": self.B3_VOCABULARY,
        }

        encoded_outbox = self.codec.encode_batch([outcome_record])
        return {
            "status": "executed",
            "reason": "ok",
            "committed_tick": self.tick,
            "committed_sha256": self.current_hash,
            "base": self.current_base,
            "sidecar": sidecar,
            "outbox_batch": encoded_outbox,
        }


# @anchor:tests.test_fx2_f9_j4_godot_scorecard_e2e_simulation.ExpectedSimulator
class ExpectedSimulator:
    """Independent oracle building expected state/hash transitions."""

    def __init__(self, seed=42):
        self.engine = FakeGodotEngine(seed=seed)

    def run_8_step_sequence(self, steps):
        transitions = []
        for action_name, args in steps:
            res = self.engine.execute_action(action_name, args, self.engine.current_hash)
            transitions.append({
                "action": action_name,
                "committed_tick": res["committed_tick"],
                "committed_sha256": res["committed_sha256"],
                "canonical_dumps": schema.dumps(res["base"]),
            })
        cmd_log_bytes = schema.dumps(self.engine.command_log).encode("utf-8")
        return transitions, cmd_log_bytes, self.engine.state_history


# @anchor:tests.test_fx2_f9_j4_godot_scorecard_e2e_simulation.TestGodotScorecardE2ESimulation
class TestGodotScorecardE2ESimulation(unittest.TestCase):
    """Offline contract test battery for perception and executor adapters."""

    def setUp(self):
        self.codec = FixtureCodec()
        self.engine = FakeGodotEngine(seed=42)
        self.eight_steps = [
            ("new_game", {"seed": 42, "country_id": "US", "difficulty": "normal", "opening_mode": "standard"}),
            ("hire", {"person_id": "p_001", "terms": {"salary": 1000}}),
            ("buy_asset", {"company_id": "c_001", "asset_type": "compute", "quantity": 10, "terms": {"price": 500}}),
            ("take_loan", {"company_id": "c_001", "terms": {"amount": 10000, "rate_bps": 500}}),
            ("advance", {"until_tick": 30}),
            ("export_chart", {"chart_id": "chart_finance_01"}),
            ("read_inbox", {"item_id": "msg_001"}),
            ("save", {}),
        ]

    def test_node_id_for_control_path_contract(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.perceive.godot_adapter import node_id_for_control_path
        path = "root/Control/Button"
        res = node_id_for_control_path(path)
        expected = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(res, expected)

    @unittest.expectedFailure
    def test_parse_ui_state_contract(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.perceive.godot_adapter import parse_ui_state
        regs = {"button": {"primary": True}}
        family, suffix = parse_ui_state("button.primary", regs)
        self.assertEqual(family, "button")
        self.assertEqual(suffix, "primary")
        with self.assertRaises(ValueError):
            parse_ui_state("invalid_token_without_reg", {})

    def test_validate_uitree_contract(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.perceive.godot_adapter import validate_uitree
        base = make_canonical_godot_base(tick=0)
        sidecar = {
            "schema": "holdings.godot.uitree/v1",
            "base_sha256": hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest(),
            "nodes": {
                hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]: {
                    "control_path": path, "screen_id": "godot",
                }
                for path in ("godot/root", "godot/button_start")
            },
        }
        violations = validate_uitree(base, sidecar)
        self.assertEqual(violations, [])

        corrupt_sidecar = dict(sidecar, base_sha256="0" * 64)
        violations_corrupt = validate_uitree(base, corrupt_sidecar)
        self.assertNotEqual(violations_corrupt, [])

    def test_read_outbox_batches_contract(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.perceive.godot_adapter import read_outbox_batches
        record = {"status": "ok", "value": 123}
        encoded = self.codec.encode_batch([record]) + b"TRAILING_UNCONSUMED"
        batches, pending = read_outbox_batches(encoded, self.codec.decode_batches)
        self.assertEqual(batches, [[record]])
        self.assertEqual(pending, b"TRAILING_UNCONSUMED")

        malformed = b"FIXTURE_ENVELOPE_V1\n{invalid json}\nCOMPLETED\n"
        with self.assertRaises(ValueError):
            read_outbox_batches(malformed, self.codec.decode_batches)

    def test_write_inbox_batch_contract(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.perceive.godot_adapter import write_inbox_batch
        sink_data = []

        def sink(data):
            sink_data.append(data)

        records = [{"tick": 1, "seq": 1, "cmd": "hire", "args": {"person_id": "p1"}}]
        write_inbox_batch(records, self.codec.encode_batch, sink)
        self.assertEqual(len(sink_data), 1)

        # Missing codec refuses initialization
        with self.assertRaises((ValueError, TypeError)):
            write_inbox_batch(records, None, sink)

    def test_godot_executor_initialization_and_dispatch(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor

        # Refuses trusted_context = "main"
        with self.assertRaises(ValueError):
            GodotExecutor(trusted_context="main", action_schemas={}, encode_batch=self.codec.encode_batch, sink=lambda x: None)

        executor = GodotExecutor(
            trusted_context={"principal": "fixture", "slot_kind": "fixture"},
            action_schemas={"hire": {"params": {"person_id": "str", "terms": "dict"}}},
            encode_batch=self.codec.encode_batch,
            sink=lambda x: None,
        )
        record = {"tick": 1, "seq": 1, "cmd": "hire", "args": {"person_id": "p_001", "terms": {"salary": 1000}}}
        res = executor.dispatch(record, committed_tick=0, basis_sha256=self.engine.current_hash)
        self.assertIn("status", res)

    @unittest.expectedFailure
    def test_e2e_8_step_simulation_and_replay_battery(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.perceive.godot_adapter import validate_uitree, read_outbox_batches
        from wender.act.godot_executor import GodotExecutor

        sink_calls = []
        executor = GodotExecutor(
            trusted_context="fixture",
            action_schemas=FakeGodotEngine.CLOSED_ACTION_SCHEMAS,
            encode_batch=self.codec.encode_batch,
            sink=lambda data: sink_calls.append(data),
        )

        executed_steps = 0
        wrong_actions = 0
        step_results = []

        for action_name, args in self.eight_steps:
            basis_sha = self.engine.current_hash
            rec = {"action": action_name, "args": args}
            disp_res = executor.dispatch(rec, committed_tick=self.engine.tick, basis_sha256=basis_sha)
            eng_res = self.engine.execute_action(action_name, args, basis_sha)
            if eng_res["status"] == "executed":
                executed_steps += 1
            else:
                wrong_actions += 1
            step_results.append((eng_res, disp_res))

        self.assertEqual(executed_steps, 8)
        self.assertEqual(wrong_actions, 0)

        # Validate base and sidecar after export_chart (step 6, index 5)
        export_res = step_results[5][0]
        base = export_res["base"]
        sidecar = export_res["sidecar"]

        self.assertEqual(schema.validate_state(base), [])
        self.assertEqual(base["source"]["kind"], "godot")
        self.assertEqual(validate_uitree(base, sidecar), [])

        # Validate outbox batch carrying chart series, committed source tick, refresh cadence, action registry, B3 vocabulary
        outbox_bytes = export_res["outbox_batch"]
        batches, _ = read_outbox_batches(outbox_bytes, self.codec.decode_batches)
        outbox_rec = batches[0][0]
        self.assertIsNotNone(outbox_rec.get("chart_series"))
        self.assertEqual(outbox_rec.get("refresh_cadence"), 5)
        self.assertEqual(outbox_rec.get("committed_tick"), 31)  # step 6 tick
        self.assertEqual(outbox_rec.get("b3_vocabulary"), FakeGodotEngine.B3_VOCABULARY)

        # Replay battery from reset seed & fixture state
        oracle = ExpectedSimulator(seed=42)
        exp_transitions, exp_cmd_log_bytes, exp_history = oracle.run_8_step_sequence(self.eight_steps)

        actual_cmd_log_bytes = schema.dumps(self.engine.command_log).encode("utf-8")
        self.assertEqual(actual_cmd_log_bytes, exp_cmd_log_bytes)
        self.assertEqual(len(self.engine.state_history), len(exp_history))

    def test_negative_control_wrong_action(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor
        executor = GodotExecutor(
            trusted_context={"principal": "fixture", "slot_kind": "fixture"},
            action_schemas={"hire": {"params": {"person_id": "str", "terms": "dict"}}},
            encode_batch=self.codec.encode_batch,
            sink=lambda x: None,
        )
        rec = {"tick": 1, "seq": 1, "cmd": "invalid_action_name", "args": {}}
        res = executor.dispatch(rec, committed_tick=self.engine.tick, basis_sha256=self.engine.current_hash)
        self.assertEqual(res.get("status"), "rejected")

    @unittest.expectedFailure
    def test_negative_control_stale_hash(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor
        executor = GodotExecutor(
            trusted_context="fixture",
            action_schemas=FakeGodotEngine.CLOSED_ACTION_SCHEMAS,
            encode_batch=self.codec.encode_batch,
            sink=lambda x: None,
        )
        stale_sha = "0" * 64
        rec = {"action": "hire", "args": {"person_id": "p_001", "terms": {"salary": 1000}}}
        res = executor.dispatch(rec, committed_tick=self.engine.tick, basis_sha256=stale_sha)
        self.assertEqual(res.get("status"), "rejected")

    def test_negative_control_missing_chart_cadence(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.perceive.godot_adapter import validate_uitree
        base = make_canonical_godot_base(tick=0)
        invalid_sidecar = {"ax_sha256": base["ax_sha256"]}  # Missing refresh_cadence
        violations = validate_uitree(base, invalid_sidecar)
        self.assertNotEqual(violations, [])

    def test_negative_control_truncated_batch(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.perceive.godot_adapter import read_outbox_batches
        truncated_data = b"FIXTURE_ENVELOPE_V1\n[{\"status\": \"incomplete\"}"  # missing COMPLETED delimiter
        batches, pending = read_outbox_batches(truncated_data, self.codec.decode_batches)
        self.assertEqual(batches, [])
        self.assertEqual(pending, truncated_data)

    def test_negative_control_duplicated_applied_outcome(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor
        executor = GodotExecutor(
            trusted_context={"principal": "fixture", "slot_kind": "fixture"},
            action_schemas={"hire": {"params": {"person_id": "str", "terms": "dict"}}},
            encode_batch=self.codec.encode_batch,
            sink=lambda x: None,
        )
        rec = {"tick": 1, "seq": 1, "cmd": "hire", "args": {"person_id": "p_001", "terms": {"salary": 1000}}}
        res1 = executor.dispatch(rec, committed_tick=self.engine.tick, basis_sha256=self.engine.current_hash)
        res2 = executor.dispatch(rec, committed_tick=self.engine.tick, basis_sha256=self.engine.current_hash)
        self.assertEqual(res2.get("status"), "rejected")

    def test_negative_control_reordered_outcome(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor
        executor = GodotExecutor(
            trusted_context={"principal": "fixture", "slot_kind": "fixture"},
            action_schemas={"hire": {"params": {"person_id": "str", "terms": "dict"}}},
            encode_batch=self.codec.encode_batch,
            sink=lambda x: None,
        )
        rec = {"tick": 1, "seq": 1, "cmd": "hire", "args": {"person_id": "p_001", "terms": {"salary": 1000}}}
        # Dispatch with tick far in future
        res = executor.dispatch(rec, committed_tick=999, basis_sha256=self.engine.current_hash)
        self.assertEqual(res.get("status"), "rejected")


if __name__ == "__main__":
    unittest.main()
