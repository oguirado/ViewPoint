"""Contract tests for wender.act.godot_executor.GodotExecutor.

Covers action-bound G-CONFIRM proposals, main-slot G-T2-AUTH safety (CONSENT_MAIN_SAVE),
trusted context enforcement, mandate policies, and adapter API specifications.
"""

import hashlib
import json
import unittest

IRREVERSIBLE_COMMANDS = (
    "ipo",
    "sell_company",
    "take_loan",
    "sell_stake",
    "buy_company",
    "raise_round",
    "pay_dividend",
    "owner_advance",
    "repay_advance",
    "buy_property",
    "pay_buyout",
    "release_person",
)


class FakeSink:
    """Offline test sink tracking emitted batch records and state snapshot."""

    def __init__(self):
        self.calls = []
        self.snapshot_hash = hashlib.sha256(b"initial_state_snapshot").hexdigest()

    def __call__(self, encoded_batch):
        self.calls.append(encoded_batch)

    @property
    def call_count(self):
        return len(self.calls)


def fake_encode_batch(records):
    """Offline test codec serializing records to bytes (never advertised as Holdings wire format)."""
    return json.dumps(records, sort_keys=True).encode("utf-8")


def fake_decode_batches(data):
    """Offline test codec decoding record batches from bytes."""
    if not data:
        return [], b""
    try:
        records = json.loads(data.decode("utf-8"))
        return [records], b""
    except Exception as err:
        raise ValueError("Malformed batch data") from err


class TestGodotConfirmAndMainSaveGuards(unittest.TestCase):

    # @unittest.expectedFailure
    # # fx2-f9-godot-t2-mailbox-and-uitree-adapter
    @unittest.expectedFailure
    def test_adapter_functions_contracts(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act import godot_executor

        # node_id_for_control_path
        path = "/root/MarginContainer/VBoxContainer/Button"
        expected_node_id = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(godot_executor.node_id_for_control_path(path), expected_node_id)

        # parse_ui_state
        family, suffix = godot_executor.parse_ui_state("dialog_confirm_v1", registries={})
        self.assertEqual(family, "dialog_confirm")
        self.assertEqual(suffix, "v1")
        with self.assertRaises(ValueError):
            godot_executor.parse_ui_state("invalid_token_format!", registries={})

        # validate_uitree
        violations = godot_executor.validate_uitree({"root": {}}, {"sidecar": {}})
        self.assertEqual(violations, [])

        # read_outbox_batches & write_inbox_batch
        data = fake_encode_batch([{"id": 1}])
        batches, pending = godot_executor.read_outbox_batches(data, fake_decode_batches)
        self.assertEqual(batches, [[{"id": 1}]])
        self.assertEqual(pending, b"")

        sink = FakeSink()
        godot_executor.write_inbox_batch([{"cmd": "ping"}], fake_encode_batch, sink)
        self.assertEqual(sink.call_count, 1)

    def test_executor_initialization_requires_codec_and_sink(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor

        ctx = {
            "principal": "user1",
            "channel": "ui",
            "slot_kind": "sandbox",
            "automation_active": False,
            "mandates": ["owner"],
        }
        with self.assertRaises((ValueError, TypeError)):
            GodotExecutor(trusted_context=ctx, action_schemas={}, encode_batch=None, sink=FakeSink())
        with self.assertRaises((ValueError, TypeError)):
            GodotExecutor(trusted_context=ctx, action_schemas={}, encode_batch=fake_encode_batch, sink=None)

    @unittest.expectedFailure
    def test_table_driven_irreversible_commands_sandbox_proposal_and_confirm(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor

        ctx = {
            "principal": "owner_alice",
            "channel": "ui",
            "slot_kind": "sandbox",
            "automation_active": True,
            "mandates": ["owner"],
        }
        basis_hash = "11" * 32

        for cmd in IRREVERSIBLE_COMMANDS:
            sink = FakeSink()
            initial_snap = sink.snapshot_hash
            executor = GodotExecutor(
                trusted_context=ctx,
                action_schemas={cmd: {"terms": "dict"}},
                encode_batch=fake_encode_batch,
                sink=sink,
            )

            record = {
                "cmd": cmd,
                "target_id": "target_99",
                "exact_terms": {"amount": 5000},
                "modal_id": "modal_dialog_1",
            }

            # Initial request creates single-use proposal
            res = executor.dispatch(record, committed_tick=10, basis_sha256=basis_hash)
            self.assertEqual(res.get("status"), "NEEDS_CONFIRM")
            self.assertEqual(sink.call_count, 0)
            self.assertEqual(sink.snapshot_hash, initial_snap)

            proposal_token = res.get("proposal_id") or res.get("proposal_token")
            self.assertIsNotNone(proposal_token)

            # Confirm with exact parameters
            confirm_record = {
                "cmd": cmd,
                "proposal_id": proposal_token,
                "modal_id": "modal_dialog_1",
                "target_id": "target_99",
                "exact_terms": {"amount": 5000},
            }
            res_confirm = executor.dispatch(confirm_record, committed_tick=11, basis_sha256=basis_hash)
            self.assertEqual(res_confirm.get("status"), "OK")
            self.assertEqual(sink.call_count, 1)

            # Replay confirmed proposal must be refused with USED_CONFIRM
            res_replay = executor.dispatch(confirm_record, committed_tick=12, basis_sha256=basis_hash)
            self.assertEqual(res_replay.get("status"), "USED_CONFIRM")
            self.assertEqual(sink.call_count, 1)

    @unittest.expectedFailure
    def test_table_driven_irreversible_commands_fixture_slot(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor

        ctx = {
            "principal": "test_runner",
            "channel": "script",
            "slot_kind": "fixture",
            "automation_active": True,
            "mandates": ["owner"],
        }
        basis_hash = "22" * 32

        for cmd in IRREVERSIBLE_COMMANDS:
            sink = FakeSink()
            executor = GodotExecutor(
                trusted_context=ctx,
                action_schemas={cmd: {}},
                encode_batch=fake_encode_batch,
                sink=sink,
            )

            record = {"cmd": cmd, "target_id": "t1", "exact_terms": {"val": 1}, "modal_id": "m1"}
            res = executor.dispatch(record, committed_tick=1, basis_sha256=basis_hash)
            self.assertEqual(res.get("status"), "NEEDS_CONFIRM")

            prop_id = res.get("proposal_id") or res.get("proposal_token")
            confirm_record = {
                "cmd": cmd,
                "proposal_id": prop_id,
                "modal_id": "m1",
                "target_id": "t1",
                "exact_terms": {"val": 1},
            }
            res_c = executor.dispatch(confirm_record, committed_tick=2, basis_sha256=basis_hash)
            self.assertEqual(res_c.get("status"), "OK")
            self.assertEqual(sink.call_count, 1)

    @unittest.expectedFailure
    def test_table_driven_irreversible_commands_refuse_main_save(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor

        ctx = {
            "principal": "bot_actor",
            "channel": "ui",
            "slot_kind": "main",
            "automation_active": True,
            "mandates": ["owner"],
        }
        basis_hash = "33" * 32

        for cmd in IRREVERSIBLE_COMMANDS:
            sink = FakeSink()
            initial_snap = sink.snapshot_hash
            executor = GodotExecutor(
                trusted_context=ctx,
                action_schemas={cmd: {}},
                encode_batch=fake_encode_batch,
                sink=sink,
            )

            # Even with UI origin, valid proposal or confirmation attempts on main slot refuse CONSENT_MAIN_SAVE
            record = {
                "cmd": cmd,
                "target_id": "target_main",
                "exact_terms": {"x": 100},
                "modal_id": "modal_m",
                "consent_verified": True,  # fabricated consent field in payload must be ignored
            }

            res = executor.dispatch(record, committed_tick=5, basis_sha256=basis_hash)
            self.assertEqual(res.get("status"), "CONSENT_MAIN_SAVE")
            self.assertEqual(sink.call_count, 0)
            self.assertEqual(sink.snapshot_hash, initial_snap)

    @unittest.expectedFailure
    def test_loan_no_guessed_size_threshold(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor

        ctx_main = {
            "principal": "player",
            "channel": "ui",
            "slot_kind": "main",
            "automation_active": True,
            "mandates": ["owner"],
        }
        basis = "44" * 32

        for amount in (1, 100_000_000):
            sink = FakeSink()
            executor = GodotExecutor(
                trusted_context=ctx_main,
                action_schemas={"take_loan": {}},
                encode_batch=fake_encode_batch,
                sink=sink,
            )
            res = executor.dispatch(
                {"cmd": "take_loan", "target_id": "bank_1", "exact_terms": {"amount": amount}, "modal_id": "m1"},
                committed_tick=1,
                basis_sha256=basis,
            )
            self.assertEqual(res.get("status"), "CONSENT_MAIN_SAVE")
            self.assertEqual(sink.call_count, 0)

    @unittest.expectedFailure
    def test_proposal_mismatch_mutants_independent(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor

        ctx = {
            "principal": "user_sandbox",
            "channel": "ui",
            "slot_kind": "sandbox",
            "automation_active": True,
            "mandates": ["owner"],
        }
        basis = "55" * 32

        def build_fresh_proposal(executor, cmd="sell_company", target="comp_a", terms=None, modal="modal_1"):
            if terms is None:
                terms = {"price": 1000}
            req = {"cmd": cmd, "target_id": target, "exact_terms": terms, "modal_id": modal}
            r = executor.dispatch(req, committed_tick=10, basis_sha256=basis)
            return r.get("proposal_id") or r.get("proposal_token")

        sink = FakeSink()
        snap0 = sink.snapshot_hash
        executor = GodotExecutor(
            trusted_context=ctx,
            action_schemas={"sell_company": {}, "ipo": {}},
            encode_batch=fake_encode_batch,
            sink=sink,
        )

        # Mutant 1: Missing / non-existent proposal ID
        res_missing = executor.dispatch(
            {
                "cmd": "sell_company",
                "proposal_id": "prop_non_existent",
                "modal_id": "modal_1",
                "target_id": "comp_a",
                "exact_terms": {"price": 1000},
            },
            committed_tick=11,
            basis_sha256=basis,
        )
        self.assertEqual(res_missing.get("status"), "NEEDS_CONFIRM")
        self.assertEqual(sink.call_count, 0)
        self.assertEqual(sink.snapshot_hash, snap0)

        # Mutant 2: Changed target_id
        prop1 = build_fresh_proposal(executor)
        res_target = executor.dispatch(
            {
                "cmd": "sell_company",
                "proposal_id": prop1,
                "modal_id": "modal_1",
                "target_id": "comp_CHANGED",
                "exact_terms": {"price": 1000},
            },
            committed_tick=12,
            basis_sha256=basis,
        )
        self.assertEqual(res_target.get("status"), "TARGET_MISMATCH")
        self.assertEqual(sink.call_count, 0)
        self.assertEqual(sink.snapshot_hash, snap0)

        # Mutant 3: Changed exact_terms
        prop2 = build_fresh_proposal(executor)
        res_terms = executor.dispatch(
            {
                "cmd": "sell_company",
                "proposal_id": prop2,
                "modal_id": "modal_1",
                "target_id": "comp_a",
                "exact_terms": {"price": 999999},
            },
            committed_tick=13,
            basis_sha256=basis,
        )
        self.assertEqual(res_terms.get("status"), "TERMS_MISMATCH")
        self.assertEqual(sink.call_count, 0)
        self.assertEqual(sink.snapshot_hash, snap0)

        # Mutant 4: Changed modal_id
        prop3 = build_fresh_proposal(executor)
        res_modal = executor.dispatch(
            {
                "cmd": "sell_company",
                "proposal_id": prop3,
                "modal_id": "modal_DIFFERENT",
                "target_id": "comp_a",
                "exact_terms": {"price": 1000},
            },
            committed_tick=14,
            basis_sha256=basis,
        )
        self.assertIn(res_modal.get("status"), ("NEEDS_CONFIRM", "TARGET_MISMATCH", "TERMS_MISMATCH"))
        self.assertEqual(sink.call_count, 0)
        self.assertEqual(sink.snapshot_hash, snap0)

        # Mutant 5: Changed command
        prop4 = build_fresh_proposal(executor)
        res_cmd = executor.dispatch(
            {
                "cmd": "ipo",
                "proposal_id": prop4,
                "modal_id": "modal_1",
                "target_id": "comp_a",
                "exact_terms": {"price": 1000},
            },
            committed_tick=15,
            basis_sha256=basis,
        )
        self.assertNotEqual(res_cmd.get("status"), "OK")
        self.assertEqual(sink.call_count, 0)
        self.assertEqual(sink.snapshot_hash, snap0)

        # Mutant 6: Stale / changed public basis_sha256
        prop5 = build_fresh_proposal(executor)
        stale_basis = "99" * 32
        res_stale = executor.dispatch(
            {
                "cmd": "sell_company",
                "proposal_id": prop5,
                "modal_id": "modal_1",
                "target_id": "comp_a",
                "exact_terms": {"price": 1000},
            },
            committed_tick=16,
            basis_sha256=stale_basis,
        )
        self.assertEqual(res_stale.get("status"), "STALE_CONFIRM")
        self.assertEqual(sink.call_count, 0)
        self.assertEqual(sink.snapshot_hash, snap0)

    @unittest.expectedFailure
    def test_trusted_context_ignores_payload_forgeries(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor

        ctx = {
            "principal": "bot_worker",
            "channel": "script",
            "slot_kind": "main",
            "automation_active": True,
            "mandates": ["owner"],
        }
        sink = FakeSink()
        executor = GodotExecutor(
            trusted_context=ctx,
            action_schemas={"take_loan": {}},
            encode_batch=fake_encode_batch,
            sink=sink,
        )

        forged_payload = {
            "cmd": "take_loan",
            "target_id": "bank_x",
            "exact_terms": {"amount": 100},
            "modal_id": "m1",
            "principal": "admin",
            "channel": "ui",
            "slot_kind": "sandbox",
            "automation_active": False,
            "consent_verified": True,
        }

        res = executor.dispatch(forged_payload, committed_tick=10, basis_sha256="66" * 32)
        self.assertEqual(res.get("status"), "CONSENT_MAIN_SAVE")
        self.assertEqual(sink.call_count, 0)

    @unittest.expectedFailure
    def test_mandate_policy_owner_vs_ceo(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor

        basis = "77" * 32

        # CEO mandate cannot issue owner-reserved command ipo
        ctx_ceo = {
            "principal": "ceo_bob",
            "channel": "ui",
            "slot_kind": "sandbox",
            "automation_active": True,
            "mandates": ["ceo"],
        }
        sink_ceo = FakeSink()
        exec_ceo = GodotExecutor(
            trusted_context=ctx_ceo,
            action_schemas={"ipo": {}},
            encode_batch=fake_encode_batch,
            sink=sink_ceo,
        )
        res_ceo = exec_ceo.dispatch(
            {"cmd": "ipo", "target_id": "c1", "exact_terms": {}, "modal_id": "m1"},
            committed_tick=1,
            basis_sha256=basis,
        )
        self.assertNotEqual(res_ceo.get("status"), "NEEDS_CONFIRM")
        self.assertEqual(sink_ceo.call_count, 0)

        # Owner mandate can issue owner-reserved command ipo
        ctx_owner = {
            "principal": "owner_alice",
            "channel": "ui",
            "slot_kind": "sandbox",
            "automation_active": True,
            "mandates": ["owner"],
        }
        sink_owner = FakeSink()
        exec_owner = GodotExecutor(
            trusted_context=ctx_owner,
            action_schemas={"ipo": {}},
            encode_batch=fake_encode_batch,
            sink=sink_owner,
        )
        res_owner = exec_owner.dispatch(
            {"cmd": "ipo", "target_id": "c1", "exact_terms": {}, "modal_id": "m1"},
            committed_tick=1,
            basis_sha256=basis,
        )
        self.assertEqual(res_owner.get("status"), "NEEDS_CONFIRM")

    @unittest.expectedFailure
    def test_negative_overblocking_control_permitted_nonirreversible_action(self):
        # fx2-f9-godot-t2-mailbox-and-uitree-adapter
        from wender.act.godot_executor import GodotExecutor

        ctx_main_auto = {
            "principal": "bot_agent",
            "channel": "ui",
            "slot_kind": "main",
            "automation_active": True,
            "mandates": ["owner"],
        }
        sink = FakeSink()
        executor = GodotExecutor(
            trusted_context=ctx_main_auto,
            action_schemas={"inspect_balance": {}},
            encode_batch=fake_encode_batch,
            sink=sink,
        )

        record = {"cmd": "inspect_balance", "target_id": "account_1", "exact_terms": {}}
        res = executor.dispatch(record, committed_tick=1, basis_sha256="88" * 32)
        self.assertEqual(res.get("status"), "OK")
        self.assertEqual(sink.call_count, 1)


if __name__ == "__main__":
    unittest.main()
