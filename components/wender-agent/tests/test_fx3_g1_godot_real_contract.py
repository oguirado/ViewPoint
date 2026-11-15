"""Regressions for the actual Holdings P1 Godot T2 adapter contract (FX3 G1)."""
import copy
import hashlib
import unittest

from wender.act.godot_executor import GodotExecutor
from wender.perceive.godot import (
    GodotError,
    is_valid_control_path,
    node_id_for_control_path,
    read_outbox_batches,
    validate_inbox_record,
    validate_uitree,
    write_inbox_batch,
)
from wender.state import schema
from tests.test_godot_t2_adapter import (
    _fixture_decode_batches,
    _fixture_encode_batch,
    _make_valid_base_and_sidecar,
)


class TestRealHoldingsGodotContract(unittest.TestCase):
    def test_slash_paths_in_real_shaped_sidecar_and_mutants(self):
        base, sidecar = _make_valid_base_and_sidecar()
        path_to_id = {}
        path_to_path = {}
        for entry in sidecar["nodes"].values():
            original = entry["control_path"]
            # A screen root, with a slash path for every actual descendant.
            new_path = "scr_home" + (
                "/" + original[len("Root."):].replace(".", "/") if original != "Root" else ""
            )
            path_to_path[original] = new_path
            path_to_id[node_id_for_control_path(original)] = node_id_for_control_path(new_path)
        for node in base["nodes"]:
            node["parent"] = path_to_id.get(node["parent"], node["parent"])
            node["id"] = path_to_id[node["id"]]
        base["actionable"] = [path_to_id[nid] for nid in base["actionable"]]
        sidecar["nodes"] = {
            path_to_id[old_id]: {
                **entry,
                "control_path": path_to_path[entry["control_path"]],
                "screen_id": "scr_home",
            }
            for old_id, entry in sidecar["nodes"].items()
        }
        sidecar["base_sha256"] = hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest()
        self.assertEqual(validate_uitree(base, sidecar), [])
        wrong_screen = copy.deepcopy(sidecar)
        wrong_screen["nodes"][node_id_for_control_path("scr_home/MainPanel")]["screen_id"] = "scr_other"
        self.assertTrue(any("control_path" in err for err in validate_uitree(base, wrong_screen)))
        for path in ("scr_home/Nav/Continue", "scr_home/Status", "scr_home/Holdings"):
            self.assertTrue(is_valid_control_path(path), path)
            self.assertEqual(node_id_for_control_path(path), hashlib.sha256(path.encode()).hexdigest()[:16])
        # The legacy dot grammar remains valid and strict.
        self.assertTrue(is_valid_control_path("Root.MainPanel.BuyBtn"))
        for mutant in (
            "/scr_home/Nav", "scr_home/", "scr_home//Nav",
            "scr_home/./Nav", "scr_home/../Nav", "scr_home/Nav/..",
            "scr_home/Nav /Continue", "scr_home/Nav\tContinue",
            "scr_home/Nav\nContinue", "scr_home/Nav\u0085Continue",
            "scr_home/Nav\x00Continue", "scr_home/Nav\u200bContinue", "scr_home/Nav/\ud800",
            "Root..Chart", "Root.Chart-1", "Root.Panel.2Btn",
        ):
            with self.subTest(mutant=repr(mutant)):
                self.assertFalse(is_valid_control_path(mutant))

    def test_real_engine_outcomes_are_closed_and_order_seq_required(self):
        real = {
            "applied_tick": 2,
            "order_seq_id": 2,
            "reason": None,
            "requested_tick": 0,
            "seq": 2,
            "state_sha256": "6be5219b7214fedc974268f9d9b391393ecc096aa4b8bf63ec274dae013fe133",
            "status": "accepted",
        }
        batches, pending = read_outbox_batches(_fixture_encode_batch([real]), _fixture_decode_batches)
        self.assertEqual(pending, b"")
        self.assertEqual(batches, [[real]])
        rejected = {**real, "status": "rejected", "reason": "TICK_WINDOW"}
        proposed = {**real, "status": "proposed"}
        for valid in (rejected, proposed):
            decoded, _ = read_outbox_batches(_fixture_encode_batch([valid]), _fixture_decode_batches)
            self.assertEqual(decoded, [[valid]])
        bad_records = []
        missing = dict(real)
        del missing["order_seq_id"]
        bad_records.append(missing)
        bad_records.extend((
            {**real, "order_seq_id": 0},
            {**real, "order_seq_id": True},
            {**real, "order_seq_id": "2"},
            {**real, "unexpected": 5},
            {**real, "state_sha256": "A" * 64},
        ))
        for bad in bad_records:
            with self.subTest(outcome=bad):
                with self.assertRaises(GodotError):
                    read_outbox_batches(_fixture_encode_batch([bad]), _fixture_decode_batches)

    def test_client_never_emits_engine_assigned_order_seq_id(self):
        clean = {"tick": 3, "seq": 1, "cmd": "advance", "args": {}}
        self.assertEqual(validate_inbox_record(clean), clean)
        for bad in ({**clean, "order_seq_id": 2}, {**clean, "args": {"order_seq_id": 2}}):
            with self.subTest(command=bad):
                with self.assertRaises(ValueError):
                    write_inbox_batch([bad], _fixture_encode_batch, lambda _: self.fail("sink must not run"))

    def test_pause_advance_current_tick_without_schema_hints(self):
        writes = []
        ex = GodotExecutor(
            {"principal": "fixture", "slot_kind": "sandbox"},
            {"advance": {"args": {}}, "pause": {"args": {}}},
            _fixture_encode_batch, writes.append,
        )
        for i, cmd in enumerate(("advance", "pause"), start=1):
            outcome = ex.dispatch(
                {"tick": 4, "seq": i, "cmd": cmd, "args": {}},
                committed_tick=4, basis_sha256="b" * 64,
            )
            self.assertEqual(outcome["status"], "admitted")
            self.assertFalse(outcome["economic_effect"])
            self.assertEqual(ex.next_seq, i + 1)
        self.assertEqual(len(writes), 2)

    def test_local_rejections_do_not_burn_sequence_single_or_batch(self):
        writes = []
        ex = GodotExecutor(
            {"principal": "fixture", "slot_kind": "sandbox"},
            {"pause": {"args": {}}, "advance": {"args": {}}},
            _fixture_encode_batch, writes.append,
        )
        def record(seq, tick, cmd="pause"):
            return {"tick": tick, "seq": seq, "cmd": cmd, "args": {}}
        for rejected in (
            record(1, 6),
            {**record(1, 5), "order_seq_id": 99},
            record(1, 5, "no_such_command"),
        ):
            outcome = ex.dispatch(rejected, committed_tick=5, basis_sha256="a" * 64)
            self.assertEqual(outcome["status"], "rejected")
            self.assertEqual(ex.next_seq, 1)
            self.assertEqual(writes, [])
        admitted = ex.dispatch(record(1, 5, "advance"), committed_tick=5, basis_sha256="a" * 64)
        self.assertEqual(admitted["seq"], 1)
        self.assertEqual(ex.next_seq, 2)
        results = ex.dispatch_batch(
            [record(2, 5), record(3, 6)],
            committed_tick=5, basis_sha256="a" * 64,
        )
        self.assertEqual([r["reason"] for r in results], ["OK", "TICK_WINDOW"])
        self.assertEqual(ex.next_seq, 3)
        third = ex.dispatch(record(3, 5), committed_tick=5, basis_sha256="a" * 64)
        self.assertEqual(third["seq"], 3)
        self.assertEqual(ex.next_seq, 4)
        emitted = []
        for payload in writes:
            batches, pending = read_outbox_batches(payload, _fixture_decode_batches)
            self.assertEqual(pending, b"")
            emitted.extend(batches[0])
        self.assertEqual([r["seq"] for r in emitted], [1, 2, 3])

    def test_failed_sink_write_rolls_back_sequence(self):
        def fail_sink(_payload):
            raise OSError("simulated unwritten mailbox")
        ex = GodotExecutor(
            {"principal": "fixture", "slot_kind": "sandbox"},
            {"advance": {"args": {}}}, _fixture_encode_batch, fail_sink,
        )
        command = {"tick": 0, "seq": 1, "cmd": "advance", "args": {}}
        with self.assertRaises(OSError):
            ex.dispatch(command, committed_tick=0, basis_sha256="f" * 64)
        self.assertEqual(ex.next_seq, 1)
        with self.assertRaises(OSError):
            ex.dispatch_batch([command], committed_tick=0, basis_sha256="f" * 64)
        self.assertEqual(ex.next_seq, 1)


if __name__ == "__main__":
    unittest.main()
