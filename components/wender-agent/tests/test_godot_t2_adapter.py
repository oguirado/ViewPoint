"""Offline acceptance tests for wender.perceive.godot and wender.act.godot_executor.

Note: The batch envelope framing used in these tests (`_OFFLINE_FIXTURE_END_MARKER`) is a
test-only offline fixture codec and is NOT the live Holdings wire format.
"""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from wender.act.godot_executor import (
    IRREVERSIBLE_COMMANDS,
    GodotExecutor,
    map_ui_action,
)
from wender.perceive.godot import (
    UITREE_SCHEMA_ID,
    GodotT2Client,
    node_id_for_control_path,
    parse_ui_state,
    read_outbox_batches,
    validate_uitree,
    write_inbox_batch,
)
from wender.state import schema

# Test-only offline fixture framing marker (never advertised as Holdings wire format).
_OFFLINE_FIXTURE_END_MARKER = b"__OFFLINE_FIXTURE_BATCH_COMPLETE__\n"


def _fixture_encode_batch(records):
    """Offline test fixture batch encoder wrapping canonical LF-delimited JSON records."""
    lines = [schema.dumps(r) for r in records]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    return payload + _OFFLINE_FIXTURE_END_MARKER


def _fixture_decode_batches(data: bytes):
    """Offline test fixture batch decoder yielding batches only after explicit completion marker."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("expected bytes")
    buf = bytes(data)
    batches = []
    marker = _OFFLINE_FIXTURE_END_MARKER
    while True:
        idx = buf.find(marker)
        if idx < 0:
            break
        chunk = buf[:idx]
        buf = buf[idx + len(marker):]
        if not chunk or not chunk.endswith(b"\n"):
            raise ValueError("malformed fixture batch chunk")
        batches.append(chunk)
    return batches, buf


def _make_valid_base_and_sidecar():
    """Construct a valid Godot wender.state/v1 base and holdings.godot.uitree/v1 sidecar."""
    paths = [
        ("Root", "application", "Holdings", 0, None, False, [0, 0, 1280, 720]),
        ("Root.MainPanel", "region", "Main Panel", 1, "Root", False, [0, 0, 1280, 720]),
        ("Root.MainPanel.BuyBtn", "button", "Buy Compute", 2, "Root.MainPanel", True, [20, 20, 120, 32]),
        ("Root.MainPanel.HoldingsTable", "table", "Portfolio Table", 2, "Root.MainPanel", False, [20, 70, 800, 300]),
        ("Root.MainPanel.HoldingsTable.Row0", "row", "Acme Corp", 3, "Root.MainPanel.HoldingsTable", False, [20, 100, 800, 28]),
        ("Root.MainPanel.HoldingsTable.Row0.CellName", "cell", "Acme Corp", 4, "Root.MainPanel.HoldingsTable.Row0", False, [20, 100, 200, 28]),
        ("Root.MainPanel.HoldingsTable.Row1", "row", "Beta Labs", 3, "Root.MainPanel.HoldingsTable", False, [20, 128, 800, 28]),
        ("Root.MainPanel.HoldingsTable.Row1.CellName", "cell", "Beta Labs", 4, "Root.MainPanel.HoldingsTable.Row1", False, [20, 128, 200, 28]),
        ("Root.MainPanel.RevenueChart", "chart", "Revenue Trend", 2, "Root.MainPanel", False, [840, 70, 400, 300]),
    ]

    nodes = []
    actionable = []
    ordinal_counts = {}
    for cpath, role, name, depth, parent_path, is_act, box in paths:
        nid = node_id_for_control_path(cpath)
        pid = node_id_for_control_path(parent_path) if parent_path else None
        key = (role, name)
        ord_idx = ordinal_counts.get(key, 0)
        ordinal_counts[key] = ord_idx + 1
        node = {
            "id": nid,
            "role": role,
            "name": name,
            "depth": depth,
            "ordinal": ord_idx,
            "parent": pid,
            "states": {},
            "actionable": is_act,
            "box": box,
        }
        nodes.append(node)
        if is_act:
            actionable.append(nid)

    base = {
        "schema": schema.SCHEMA_ID,
        "source": {"kind": "godot", "url": "godot://holdings/main", "title": "Holdings"},
        "prune": {"name": "prune_ax_v1", "version": 1},
        "ax_sha256": "a" * 64,
        "ax_stable_sha256": "b" * 64,
        "viewport": {"w": 1280, "h": 720, "scroll_x": 0, "scroll_y": 0, "doc_w": 1280, "doc_h": 720, "dpr_ppm": 1000000},
        "nodes": nodes,
        "actionable": actionable,
        "counts": {"raw": 14, "nodes": len(nodes), "actionable": len(actionable)},
    }

    base_sha = hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest()

    sc_nodes = {
        node_id_for_control_path("Root"): {
            "control_path": "Root",
            "screen_id": "portfolio_overview",
        },
        node_id_for_control_path("Root.MainPanel"): {
            "control_path": "Root.MainPanel",
            "screen_id": "portfolio_overview",
        },
        node_id_for_control_path("Root.MainPanel.BuyBtn"): {
            "control_path": "Root.MainPanel.BuyBtn",
            "screen_id": "portfolio_overview",
        },
        node_id_for_control_path("Root.MainPanel.HoldingsTable"): {
            "control_path": "Root.MainPanel.HoldingsTable",
            "screen_id": "portfolio_overview",
            "table_id": "tbl_portfolio",
            "visible_range": {
                "first_row_id": "row_acme",
                "last_row_id": "row_beta",
                "count": 2,
            },
            "query": {
                "sort_key": "name",
                "sort_dir": "asc",
                "filter_spec": [
                    {"col_key": "sector", "op": "eq", "value": "ai"},
                ],
                "page_index": 0,
                "page_size": 25,
            },
            "as_of_tick": 12,
            "refresh_cadence": "tick",
        },
        node_id_for_control_path("Root.MainPanel.HoldingsTable.Row0"): {
            "control_path": "Root.MainPanel.HoldingsTable.Row0",
            "screen_id": "portfolio_overview",
            "table_id": "tbl_portfolio",
            "row_id": "row_acme",
        },
        node_id_for_control_path("Root.MainPanel.HoldingsTable.Row0.CellName"): {
            "control_path": "Root.MainPanel.HoldingsTable.Row0.CellName",
            "screen_id": "portfolio_overview",
            "table_id": "tbl_portfolio",
            "row_id": "row_acme",
            "col_key": "name",
        },
        node_id_for_control_path("Root.MainPanel.HoldingsTable.Row1"): {
            "control_path": "Root.MainPanel.HoldingsTable.Row1",
            "screen_id": "portfolio_overview",
            "table_id": "tbl_portfolio",
            "row_id": "row_beta",
        },
        node_id_for_control_path("Root.MainPanel.HoldingsTable.Row1.CellName"): {
            "control_path": "Root.MainPanel.HoldingsTable.Row1.CellName",
            "screen_id": "portfolio_overview",
            "table_id": "tbl_portfolio",
            "row_id": "row_beta",
            "col_key": "name",
        },
        node_id_for_control_path("Root.MainPanel.RevenueChart"): {
            "control_path": "Root.MainPanel.RevenueChart",
            "screen_id": "portfolio_overview",
            "as_of_tick": 12,
            "refresh_cadence": "quarterly",
        },
    }

    sidecar = {
        "schema": UITREE_SCHEMA_ID,
        "base_sha256": base_sha,
        "nodes": sc_nodes,
    }
    return base, sidecar


def _make_action_schemas():
    schemas = {
        "buy_asset": {
            "kind": "economic",
            "args": {"asset_type": "nonempty_str", "quantity": "pos_int"},
            "required": ["asset_type", "quantity"],
        },
        "start_process": {
            "kind": "economic",
            "args": {"recipe_id": "nonempty_str"},
            "required": ["recipe_id"],
        },
        "new_game": {
            "kind": "current",
            "boundary": "current",
            "economic": False,
            "args": {"seed": "nonneg_int"},
            "required": ["seed"],
        },
        "set_view": {
            "kind": "view",
            "boundary": "current",
            "economic": False,
            "args": {"screen_id": "nonempty_str"},
            "required": ["screen_id"],
        },
        "save": {
            "kind": "at_rest",
            "boundary": "current",
            "economic": False,
            "args": {"slot_name": "nonempty_str"},
            "required": ["slot_name"],
        },
        "unpublished_op": {
            "published": False,
            "args": {},
        },
        "unresolved_op": None,
    }
    for cmd in IRREVERSIBLE_COMMANDS:
        schemas[cmd] = {
            "kind": "economic",
            "irreversible": True,
            "args": {
                "target_id": "nonempty_str",
                "terms": "dict",
            },
            "required": ["target_id", "terms"],
        }
    return schemas


class TestGodotPerceiveAndUiTree(unittest.TestCase):
    def test_node_id_for_control_path_sha256_prefix(self):
        path = "Root.Panel.Btn"
        expected = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(node_id_for_control_path(path), expected)
        self.assertEqual(len(node_id_for_control_path(path)), 16)
        with self.assertRaises(ValueError):
            node_id_for_control_path(123)  # type: ignore[arg-type]

    def test_parse_ui_state_all_b3_families_and_rejections(self):
        registries = {
            "modal": {"confirm_loan", "confirm_ipo"},
            "busy": {"saving_snapshot", "sim_step"},
            "error": {"E_INSUFFICIENT_FUNDS", "E_SAVE_IO"},
        }
        self.assertEqual(parse_ui_state("simulating", registries), ("simulating", None))
        self.assertEqual(parse_ui_state("paused", registries), ("paused", None))
        self.assertEqual(parse_ui_state("awaiting_input", registries), ("awaiting_input", None))
        self.assertEqual(parse_ui_state("modal:confirm_loan", registries), ("modal", "confirm_loan"))
        self.assertEqual(parse_ui_state("busy:saving_snapshot", registries), ("busy", "saving_snapshot"))
        self.assertEqual(parse_ui_state("error:E_INSUFFICIENT_FUNDS", registries), ("error", "E_INSUFFICIENT_FUNDS"))

        invalid_tokens = [
            "",
            "unknown_family",
            "simulating:",
            "simulating:extra",
            "paused:now",
            "awaiting_input:1",
            "modal",
            "modal:",
            "modal:unregistered_modal",
            "modal:confirm_loan:extra",
            "busy",
            "busy:",
            "busy:unknown_op",
            "error",
            "error:",
            "error:UNREGISTERED_ERR",
        ]
        for bad in invalid_tokens:
            with self.assertRaises(ValueError, msg=f"Should reject token {bad!r}"):
                parse_ui_state(bad, registries)

    def test_validate_uitree_valid_and_invalid_linkage_controls(self):
        base, sidecar = _make_valid_base_and_sidecar()
        self.assertEqual(schema.validate_state(base), [])
        self.assertEqual(
            validate_uitree(
                base,
                sidecar,
                cadence_registry={"tick", "quarterly"},
                scene_inventory={"portfolio_overview"},
            ),
            [],
        )

        # 1. Digest mismatch
        bad_digest = copy.deepcopy(sidecar)
        bad_digest["base_sha256"] = "0" * 64
        self.assertTrue(any("digest mismatch" in e for e in validate_uitree(base, bad_digest)))

        # 2. Missing and orphan sidecar nodes
        missing_sc = copy.deepcopy(sidecar)
        missing_sc["nodes"].pop(node_id_for_control_path("Root.MainPanel.BuyBtn"))
        self.assertTrue(any("missing" in e for e in validate_uitree(base, missing_sc)))

        orphan_sc = copy.deepcopy(sidecar)
        orphan_id = node_id_for_control_path("Root.Orphan")
        orphan_sc["nodes"][orphan_id] = {"control_path": "Root.Orphan", "screen_id": "portfolio_overview"}
        self.assertTrue(any("orphan" in e for e in validate_uitree(base, orphan_sc)))

        # 3. Invalid stable scoped paths: Root.Panel.2Btn, Root..Btn, Root.Btn-1
        for bad_path in ("Root.Panel.2Btn", "Root..Btn", "Root.Btn-1"):
            b_mut, s_mut = _make_valid_base_and_sidecar()
            old_id = node_id_for_control_path("Root.MainPanel.BuyBtn")
            new_id = node_id_for_control_path(bad_path)
            for n in b_mut["nodes"]:
                if n["id"] == old_id:
                    n["id"] = new_id
            b_mut["actionable"] = [new_id]
            b_mut_sha = hashlib.sha256(schema.dumps(b_mut).encode("utf-8")).hexdigest()
            s_mut["base_sha256"] = b_mut_sha
            entry = s_mut["nodes"].pop(old_id)
            entry["control_path"] = bad_path
            s_mut["nodes"][new_id] = entry
            errs = validate_uitree(b_mut, s_mut)
            self.assertTrue(any("control_path" in e for e in errs), f"Expected rejection for {bad_path!r}: {errs}")

        # 4. Summary classification from declared role and linkage (not presence of fields)
        btn_id = node_id_for_control_path("Root.MainPanel.BuyBtn")
        btn_with_summary = copy.deepcopy(sidecar)
        btn_with_summary["nodes"][btn_id]["as_of_tick"] = 12
        btn_with_summary["nodes"][btn_id]["refresh_cadence"] = "tick"
        self.assertTrue(any("forbids" in e for e in validate_uitree(base, btn_with_summary)))

        tbl_id = node_id_for_control_path("Root.MainPanel.HoldingsTable")
        tbl_missing_query = copy.deepcopy(sidecar)
        del tbl_missing_query["nodes"][tbl_id]["query"]
        self.assertTrue(any("missing required field 'query'" in e for e in validate_uitree(base, tbl_missing_query)))

        # 5. Inconsistent table/row/cell linkage
        cell_id = node_id_for_control_path("Root.MainPanel.HoldingsTable.Row0.CellName")
        bad_cell = copy.deepcopy(sidecar)
        bad_cell["nodes"][cell_id]["row_id"] = "nonexistent_row"
        self.assertTrue(any("row" in e for e in validate_uitree(base, bad_cell)))

        # 6. Unknown fields rejected
        unk_sc = copy.deepcopy(sidecar)
        unk_sc["nodes"][btn_id]["unexpected_field"] = "bad"
        self.assertTrue(any("unknown fields" in e for e in validate_uitree(base, unk_sc)))


class TestGodotT2MailboxAndTransport(unittest.TestCase):
    def test_transport_enabled_off_and_refusal_controls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            inbox = Path(tmpdir) / "t2-inbox.jsonl"
            outbox = Path(tmpdir) / "t2-outbox.jsonl"

            # OFF mode when both endpoints omitted
            off_client = GodotT2Client()
            self.assertFalse(off_client.enabled)
            self.assertEqual(off_client.mode, "OFF")
            self.assertEqual(off_client.poll_outbox(b"ignored"), ([], b""))
            self.assertIsNone(off_client.send_inbox_batch([{"tick": 1, "seq": 1, "cmd": "set_view", "args": {}}]))

            # Partial pairs refuse initialization
            with self.assertRaises(ValueError):
                GodotT2Client(t2_inbox=inbox, t2_outbox=None, publisher="engine", decode_batches=_fixture_decode_batches, encode_batch=_fixture_encode_batch)
            with self.assertRaises(ValueError):
                GodotT2Client(t2_inbox=None, t2_outbox=outbox, publisher="engine", decode_batches=_fixture_decode_batches, encode_batch=_fixture_encode_batch)

            # Coincident endpoints refuse initialization
            with self.assertRaises(ValueError):
                GodotT2Client(t2_inbox=inbox, t2_outbox=inbox, publisher="engine", decode_batches=_fixture_decode_batches, encode_batch=_fixture_encode_batch)

            # Missing publisher or missing codec refuses initialization
            with self.assertRaises(ValueError):
                GodotT2Client(t2_inbox=inbox, t2_outbox=outbox, publisher=None, decode_batches=_fixture_decode_batches, encode_batch=_fixture_encode_batch)
            with self.assertRaises(ValueError):
                GodotT2Client(t2_inbox=inbox, t2_outbox=outbox, publisher="engine", decode_batches=None, encode_batch=_fixture_encode_batch)

            # Enabled mode succeeds with distinct endpoints, publisher, and codecs
            client = GodotT2Client(
                t2_inbox=inbox,
                t2_outbox=outbox,
                publisher="holdings_t2_fixture_pub",
                decode_batches=_fixture_decode_batches,
                encode_batch=_fixture_encode_batch,
            )
            self.assertTrue(client.enabled)

    def test_read_outbox_batches_split_truncated_and_malformed(self):
        base, sidecar = _make_valid_base_and_sidecar()
        raw_batch = _fixture_encode_batch([base, sidecar])
        split_at = len(raw_batch) // 2

        # Truncated first half: no partial batch exposed, bytes stay pending
        part1 = raw_batch[:split_at]
        part2 = raw_batch[split_at:]
        batches1, pending1 = read_outbox_batches(part1, _fixture_decode_batches)
        self.assertEqual(batches1, [])
        self.assertEqual(pending1, part1)

        # Completing the batch yields the full batch and empty pending tail
        batches2, pending2 = read_outbox_batches(pending1 + part2, _fixture_decode_batches)
        self.assertEqual(len(batches2), 1)
        self.assertEqual(len(batches2[0]), 2)
        self.assertEqual(pending2, b"")

        # Duplicate JSON key in batch rejects the whole malformed batch
        dup_key_batch = b'{"tick":1,"tick":2}\n' + _OFFLINE_FIXTURE_END_MARKER
        with self.assertRaises(ValueError):
            read_outbox_batches(dup_key_batch, _fixture_decode_batches)

        # Float in batch rejects the whole malformed batch
        float_batch = b'{"tick":1.5}\n' + _OFFLINE_FIXTURE_END_MARKER
        with self.assertRaises(ValueError):
            read_outbox_batches(float_batch, _fixture_decode_batches)

        # Unicode line separator U+2028 between objects is not treated as record delimiter
        u2028_batch = '{"tick":1}\u2028{"tick":2}\n'.encode("utf-8") + _OFFLINE_FIXTURE_END_MARKER
        with self.assertRaises(ValueError):
            read_outbox_batches(u2028_batch, _fixture_decode_batches)

    def test_write_inbox_batch_validates_all_before_single_sink_call(self):
        calls = []
        valid_rec = {"tick": 2, "seq": 1, "cmd": "buy_asset", "args": {"asset_type": "gpu", "quantity": 2}}
        out = write_inbox_batch([valid_rec], _fixture_encode_batch, calls.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], out)

        # Second record invalid (has forbidden authority field 'principal'): sink must not be called
        calls.clear()
        bad_rec = {"tick": 2, "seq": 2, "cmd": "buy_asset", "args": {"asset_type": "gpu", "quantity": 1}, "principal": "injected"}
        with self.assertRaises(ValueError):
            write_inbox_batch([valid_rec, bad_rec], _fixture_encode_batch, calls.append)
        self.assertEqual(calls, [])


class TestGodotExecutorAdmissionAndGuards(unittest.TestCase):
    def setUp(self):
        self.sink_batches = []
        self.schemas = _make_action_schemas()
        self.basis = "c" * 64
        self.trusted_ctx = {
            "principal": "operator_1",
            "channel": "t2",
            "slot_kind": "sandbox",
            "automation_active": True,
            "mandates": {"owner": True},
        }

    def _make_executor(self, ctx_overrides=None):
        ctx = copy.deepcopy(self.trusted_ctx)
        if ctx_overrides:
            ctx.update(ctx_overrides)
        return GodotExecutor(ctx, self.schemas, _fixture_encode_batch, self.sink_batches.append)

    def test_ui_action_mapping_and_canonical_rejection(self):
        mapped_buy = map_ui_action({"tick": 2, "seq": 1, "cmd": "buy_compute", "args": {"asset_type": "h100_cluster", "quantity": 1}})
        self.assertEqual(mapped_buy["cmd"], "buy_asset")
        self.assertEqual(mapped_buy["args"]["asset_type"], "h100_cluster")

        mapped_train = map_ui_action({"tick": 2, "seq": 2, "cmd": "start_training", "args": {"recipe_id": "llm_pretrain_v1"}})
        self.assertEqual(mapped_train["cmd"], "start_process")
        self.assertEqual(mapped_train["args"]["recipe_id"], "llm_pretrain_v1")

        # Unmapped buy_compute and start_training are rejected as canonical commands
        ex = self._make_executor()
        r1 = ex.dispatch({"tick": 2, "seq": 1, "cmd": "buy_compute", "args": {"asset_type": "h100", "quantity": 1}}, committed_tick=1, basis_sha256=self.basis)
        self.assertEqual(r1["reason"], "ACTION_UNAVAILABLE")
        r2 = ex.dispatch({"tick": 2, "seq": 1, "cmd": "start_training", "args": {"recipe_id": "r1"}}, committed_tick=1, basis_sha256=self.basis)
        self.assertEqual(r2["reason"], "ACTION_UNAVAILABLE")

        # Mapped commands are admitted
        r3 = ex.dispatch(mapped_buy, committed_tick=1, basis_sha256=self.basis)
        self.assertEqual(r3["status"], "admitted")
        r4 = ex.dispatch(mapped_train, committed_tick=1, basis_sha256=self.basis)
        self.assertEqual(r4["status"], "admitted")
        self.assertEqual(len(self.sink_batches), 2)

    def test_tick_window_sequence_and_batch_predecessor_preservation(self):
        ex = self._make_executor()

        # Sequence gap at start (seq=2 when expecting 1) -> SEQUENCE
        r_gap = ex.dispatch({"tick": 6, "seq": 2, "cmd": "buy_asset", "args": {"asset_type": "gpu", "quantity": 1}}, committed_tick=5, basis_sha256=self.basis)
        self.assertEqual(r_gap["reason"], "SEQUENCE")

        # Economic command at wrong tick refuses without consuming sequence 1.
        r_tick = ex.dispatch({"tick": 5, "seq": 1, "cmd": "buy_asset", "args": {"asset_type": "gpu", "quantity": 1}}, committed_tick=5, basis_sha256=self.basis)
        self.assertEqual(r_tick["reason"], "TICK_WINDOW")
        self.assertEqual(ex.next_seq, 1)

        # Retry the same seq with a valid tick: admit and only now consume seq=1.
        r_retry = ex.dispatch({"tick": 6, "seq": 1, "cmd": "buy_asset", "args": {"asset_type": "gpu", "quantity": 1}}, committed_tick=5, basis_sha256=self.basis)
        self.assertEqual(r_retry["status"], "admitted")
        self.assertEqual(ex.next_seq, 2)
        r_dup = ex.dispatch({"tick": 6, "seq": 1, "cmd": "buy_asset", "args": {"asset_type": "gpu", "quantity": 1}}, committed_tick=5, basis_sha256=self.basis)
        self.assertEqual(r_dup["reason"], "DUPLICATE_SEQ")

        # Current-boundary commands (new_game, set_view, save) use committed_tick without economic effects
        r_view = ex.dispatch({"tick": 5, "seq": 2, "cmd": "set_view", "args": {"screen_id": "market"}}, committed_tick=5, basis_sha256=self.basis)
        self.assertEqual(r_view["status"], "admitted")
        self.assertFalse(r_view["economic_effect"])

        # Duplicate identities within a batch all reject before effects
        self.sink_batches.clear()
        dup_batch = [
            {"tick": 6, "seq": 3, "cmd": "buy_asset", "args": {"asset_type": "gpu", "quantity": 1}},
            {"tick": 6, "seq": 3, "cmd": "buy_asset", "args": {"asset_type": "gpu", "quantity": 2}},
        ]
        dup_res = ex.dispatch_batch(dup_batch, committed_tick=5, basis_sha256=self.basis)
        self.assertEqual([r["reason"] for r in dup_res], ["DUPLICATE_SEQ", "DUPLICATE_SEQ"])
        self.assertEqual(self.sink_batches, [])

        # Well-formed batch preserves valid predecessors when a later command is rejected
        mixed_batch = [
            {"tick": 6, "seq": 3, "cmd": "buy_asset", "args": {"asset_type": "gpu", "quantity": 1}},
            {"tick": 99, "seq": 4, "cmd": "buy_asset", "args": {"asset_type": "gpu", "quantity": 1}},
        ]
        mixed_res = ex.dispatch_batch(mixed_batch, committed_tick=5, basis_sha256=self.basis)
        self.assertEqual(mixed_res[0]["status"], "admitted")
        self.assertEqual(mixed_res[1]["reason"], "TICK_WINDOW")
        self.assertEqual(len(self.sink_batches), 1)
        decoded, _ = read_outbox_batches(self.sink_batches[0], _fixture_decode_batches)
        self.assertEqual(len(decoded[0]), 1)
        self.assertEqual(decoded[0][0]["seq"], 3)
        self.assertEqual(ex.next_seq, 4)  # Rejected seq=4 was not emitted.

    def test_all_irreversible_commands_and_confirmation_binding_guards(self):
        ex = self._make_executor()
        for cmd in sorted(IRREVERSIBLE_COMMANDS):
            self.sink_batches.clear()
            seq = ex.next_seq
            init_rec = {
                "tick": 11,
                "seq": seq,
                "cmd": cmd,
                "args": {"target_id": f"target_{cmd}", "terms": {"amount": 500}},
            }
            res_init = ex.dispatch(init_rec, committed_tick=10, basis_sha256=self.basis)
            self.assertEqual(res_init["reason"], "NEEDS_CONFIRM", cmd)
            self.assertIn("proposal", res_init)
            self.assertEqual(self.sink_batches, [], f"Proposal for {cmd} must not emit to sink")
            self.assertEqual(ex.next_seq, seq)
            proposal = res_init["proposal"]

            # Rejected confirmation mutants do not consume the engine sequence.
            bad_target = copy.deepcopy(proposal)
            bad_target["target_id"] = "wrong_target"
            bad_terms = copy.deepcopy(proposal)
            bad_terms["terms"] = {"amount": 999999}
            bad_cmd = copy.deepcopy(proposal)
            bad_cmd["cmd"] = "other_cmd"
            for altered, basis, expected in (
                (bad_target, self.basis, "TARGET_MISMATCH"),
                (bad_terms, self.basis, "TERMS_MISMATCH"),
                (proposal, "d" * 64, "STALE_CONFIRM"),
                (bad_cmd, self.basis, None),
            ):
                outcome = ex.dispatch(
                    {"tick": 11, "seq": seq, "cmd": cmd, "args": {"confirm": altered}},
                    committed_tick=10, basis_sha256=basis,
                )
                if expected is None:
                    self.assertEqual(outcome["status"], "rejected")
                else:
                    self.assertEqual(outcome["reason"], expected)
                self.assertEqual(ex.next_seq, seq)
                self.assertEqual(self.sink_batches, [])

            # Valid confirmation is the only request written to the mailbox.
            r_ok = ex.dispatch(
                {"tick": 11, "seq": seq, "cmd": cmd, "args": {"confirm": proposal}},
                committed_tick=10, basis_sha256=self.basis,
            )
            self.assertEqual(r_ok["status"], "admitted", cmd)
            self.assertEqual(ex.next_seq, seq + 1)
            self.assertEqual(len(self.sink_batches), 1, cmd)
            batches, _ = read_outbox_batches(self.sink_batches[0], _fixture_decode_batches)
            self.assertEqual(batches[0][0]["cmd"], cmd)
            self.assertEqual(batches[0][0]["seq"], seq)
            self.assertEqual(batches[0][0]["args"], {"target_id": f"target_{cmd}", "terms": {"amount": 500}})

            # Reusing a confirmation refuses without consuming the next seq.
            r_used = ex.dispatch(
                {"tick": 11, "seq": seq + 1, "cmd": cmd, "args": {"confirm": proposal}},
                committed_tick=10, basis_sha256=self.basis,
            )
            self.assertEqual(r_used["reason"], "USED_CONFIRM", cmd)
            self.assertEqual(ex.next_seq, seq + 1)
            self.assertEqual(len(self.sink_batches), 1)

    def test_main_save_automated_irreversible_refusal_and_owner_mandates(self):
        # Automated main slot refuses irreversible commands across UI origin, fabricated consent, and confirmation
        main_ex = self._make_executor({"slot_kind": "main", "channel": "ui", "automation_active": True})
        r1 = main_ex.dispatch(
            {"tick": 2, "seq": 1, "cmd": "take_loan", "args": {"target_id": "bank_a", "terms": {"principal": 100}}},
            committed_tick=1,
            basis_sha256=self.basis,
        )
        self.assertEqual(r1["reason"], "CONSENT_MAIN_SAVE")

        # Fabricated consent field in payload still refuses CONSENT_MAIN_SAVE
        r2 = main_ex.dispatch(
            {
                "tick": 2,
                "seq": 1,
                "cmd": "take_loan",
                "args": {"target_id": "bank_a", "terms": {"principal": 100}, "consent": "self_granted"},
            },
            committed_tick=1,
            basis_sha256=self.basis,
        )
        self.assertEqual(r2["reason"], "CONSENT_MAIN_SAVE")

        # Even a pre-structured confirmation on automated main slot refuses CONSENT_MAIN_SAVE
        r3 = main_ex.dispatch(
            {
                "tick": 2,
                "seq": 1,
                "cmd": "take_loan",
                "args": {
                    "confirm": {
                        "modal_id": "modal_take_loan_1",
                        "cmd": "take_loan",
                        "target_id": "bank_a",
                        "terms": {"principal": 100},
                        "basis_sha256": self.basis,
                    }
                },
            },
            committed_tick=1,
            basis_sha256=self.basis,
        )
        self.assertEqual(r3["reason"], "CONSENT_MAIN_SAVE")
        self.assertEqual(self.sink_batches, [])

        # Owner-reserved command with CEO-only mandate refuses even when targeting own company
        ceo_ex = self._make_executor({"mandates": {"role": "ceo"}})
        r_owner = ceo_ex.dispatch(
            {"tick": 2, "seq": 1, "cmd": "pay_dividend", "args": {"target_id": "holding_self", "terms": {"amount": 1000}}},
            committed_tick=1,
            basis_sha256=self.basis,
        )
        self.assertEqual(r_owner["status"], "rejected")
        self.assertEqual(r_owner["reason"], "MANDATE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
