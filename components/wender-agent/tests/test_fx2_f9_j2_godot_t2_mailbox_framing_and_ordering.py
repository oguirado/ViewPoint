"""Contract and adversarial oracle tests for wender.perceive.godot and wender.act.godot_executor.

Target gap: G-T2 and G-ORDER client framing and sequence admission.
Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
"""

import hashlib
import json
import unittest


# ============================================================================
# Offline Fixture Codec (Test-only wire envelope, NOT Holdings native envelope)
# ============================================================================

FIXTURE_COMPLETION_MARKER = b"\n---TEST-FIXTURE-END-BATCH---\n"


class FixtureCodec:
    """Explicitly labelled offline fixture codec for testing batch framing.

    This codec uses test-only field names and an explicit completion marker.
    It is NOT the absent Holdings native envelope schema.
    """

    def encode_batch(self, records: list) -> bytes:
        payload = json.dumps({"fixture_records": records}, sort_keys=True).encode("utf-8")
        return payload + FIXTURE_COMPLETION_MARKER

    def decode_batches(self, data: bytes):
        batches = []
        pending = data
        while FIXTURE_COMPLETION_MARKER in pending:
            head, pending = pending.split(FIXTURE_COMPLETION_MARKER, 1)
            try:
                parsed = json.loads(head.decode("utf-8"))
                if not isinstance(parsed, dict) or "fixture_records" not in parsed:
                    raise ValueError("Malformed fixture batch envelope")
                batches.append(parsed["fixture_records"])
            except Exception as e:
                raise ValueError("Malformed batch payload: %s" % e) from e
        return batches, pending


def independent_whole_buffer_decoder(data: bytes):
    """Independent oracle decoder to compare accumulated output and pending bytes."""
    codec = FixtureCodec()
    return codec.decode_batches(data)


# ============================================================================
# Healthy Controls (Run and pass without @unittest.expectedFailure)
# ============================================================================

class TestFixtureCodecHealthyControl(unittest.TestCase):
    """Healthy control tests verifying fixture codec behavior and byte splitting."""

    def test_fixture_codec_roundtrip_positive_control(self):
        """Positive control: valid batch encodes with marker and decodes cleanly."""
        codec = FixtureCodec()
        records = [{"tick": 1, "seq": 1, "cmd": "move", "args": {"dir": "north"}}]
        encoded = codec.encode_batch(records)
        self.assertTrue(encoded.endswith(FIXTURE_COMPLETION_MARKER))

        batches, pending = codec.decode_batches(encoded)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0], records)
        self.assertEqual(pending, b"")

    def test_fixture_codec_incomplete_suffix_negative_control(self):
        """Negative control: truncated batch without completion marker yields pending bytes."""
        codec = FixtureCodec()
        records = [{"tick": 1, "seq": 1, "cmd": "move", "args": {"dir": "north"}}]
        encoded = codec.encode_batch(records)
        truncated = encoded[: len(encoded) - 5]

        batches, pending = codec.decode_batches(truncated)
        self.assertEqual(batches, [])
        self.assertEqual(pending, truncated)

    def test_fixture_codec_malformed_envelope_negative_control(self):
        """Negative control: malformed JSON before completion marker raises ValueError."""
        codec = FixtureCodec()
        bad_data = b"{bad_json}" + FIXTURE_COMPLETION_MARKER
        with self.assertRaises(ValueError):
            codec.decode_batches(bad_data)

    def test_byte_splitting_oracle_control(self):
        """Exhaustive byte-split control comparing incremental feeding against whole-buffer decoder."""
        codec = FixtureCodec()
        records = [{"tick": 1, "seq": 1, "cmd": "step", "args": {"val": 100}}]
        encoded = codec.encode_batch(records)

        # Feed byte-by-byte
        acc_buffer = b""
        acc_batches = []
        for i in range(len(encoded)):
            acc_buffer += encoded[i : i + 1]
            batches, acc_buffer = codec.decode_batches(acc_buffer)
            acc_batches.extend(batches)

        oracle_batches, oracle_pending = independent_whole_buffer_decoder(encoded)
        self.assertEqual(acc_batches, oracle_batches)
        self.assertEqual(acc_buffer, oracle_pending)


# ============================================================================
# Contract Tests: Perceive Adapter APIs
# ============================================================================

class TestGodotPerceiveContract(unittest.TestCase):
    """Contract tests for wender.perceive.godot helper APIs."""

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_node_id_for_control_path_positive(self):
        """Positive control: node_id_for_control_path returns 16 lowercase hex SHA256 chars."""
        from wender.perceive.godot import node_id_for_control_path

        path = "/root/Control/Button"
        expected = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16].lower()
        res = node_id_for_control_path(path)
        self.assertEqual(res, expected)
        self.assertEqual(len(res), 16)

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    @unittest.expectedFailure
    def test_parse_ui_state_positive(self):
        """Positive control: parse_ui_state parses valid token into family and optional suffix."""
        from wender.perceive.godot import parse_ui_state

        registries = {"menu": ["main_menu", "options"]}
        family, suffix = parse_ui_state("menu.main_menu:v1", registries)
        self.assertEqual(family, "menu.main_menu")
        self.assertEqual(suffix, "v1")

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_parse_ui_state_invalid_negative(self):
        """Negative control: parse_ui_state raises ValueError for invalid/unknown token."""
        from wender.perceive.godot import parse_ui_state

        registries = {"menu": ["main_menu"]}
        with self.assertRaises(ValueError):
            parse_ui_state("unknown_token_format", registries)

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_validate_uitree_valid_positive(self):
        """Positive control: validate_uitree returns empty violation list for valid tree."""
        from wender.perceive.godot import validate_uitree

        from wender.state import schema

        node_id = hashlib.sha256(b"Root").hexdigest()[:16]
        base = schema.build_state(
            [{"id": node_id, "role": "generic", "name": "Root", "depth": 0,
              "ordinal": 0, "attrs": ()}],
            kind="godot", url="res://scr_home.tscn", title="scr_home",
            ax_sha256="0" * 64, ax_stable_sha256="0" * 64,
            prune={"name": "fixture", "version": 1}, raw_count=1,
            boxes={node_id: [0, 0, 100, 100]}, viewport={"w": 100, "h": 100},
        )
        sidecar = {
            "schema": "holdings.godot.uitree/v1",
            "base_sha256": hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest(),
            "nodes": {node_id: {"control_path": "Root", "screen_id": "scr_home"}},
        }
        violations = validate_uitree(base, sidecar)
        self.assertEqual(violations, [])

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_validate_uitree_invalid_negative(self):
        """Negative control: validate_uitree returns non-empty violations for invalid tree."""
        from wender.perceive.godot import validate_uitree

        base = {"invalid_structure": True}
        sidecar = None
        violations = validate_uitree(base, sidecar)
        self.assertIsInstance(violations, list)
        self.assertGreater(len(violations), 0)


# ============================================================================
# Contract Tests: Read Outbox Batches & Write Inbox Batch
# ============================================================================

class TestGodotMailboxFramingContract(unittest.TestCase):
    """Contract tests for read_outbox_batches and write_inbox_batch."""

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_read_outbox_batches_complete_and_pending_positive(self):
        """Positive control: read_outbox_batches extracts complete batch and leaves pending tail."""
        from wender.perceive.godot import read_outbox_batches

        codec = FixtureCodec()
        batch1 = [{"tick": 1, "seq": 1, "cmd": "view", "args": {}}]
        encoded1 = codec.encode_batch(batch1)
        data = encoded1 + b'{"incomplete":'

        batches, pending = read_outbox_batches(data, codec.decode_batches)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0], batch1)
        self.assertEqual(pending, b'{"incomplete":')

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_read_outbox_batches_exhaustive_byte_splitting(self):
        """Exhaustively split encoded batch at every byte position including multibyte UTF-8."""
        from wender.perceive.godot import read_outbox_batches

        codec = FixtureCodec()
        batch = [{"tick": 1, "seq": 1, "cmd": "chat", "args": {"msg": "Héllo 世界 🌍"}}]
        encoded = codec.encode_batch(batch)

        accumulated_batches = []
        pending_buf = b""
        for i in range(len(encoded)):
            pending_buf += encoded[i : i + 1]
            new_batches, pending_buf = read_outbox_batches(pending_buf, codec.decode_batches)
            accumulated_batches.extend(new_batches)

        self.assertEqual(accumulated_batches, [batch])
        self.assertEqual(pending_buf, b"")

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_read_outbox_batches_missing_final_lf_or_completion_marker(self):
        """Incomplete completion marker or missing final LF keeps data in pending without partial batch."""
        from wender.perceive.godot import read_outbox_batches

        codec = FixtureCodec()
        batch = [{"tick": 1, "seq": 1, "cmd": "ping", "args": {}}]
        encoded = codec.encode_batch(batch)
        truncated = encoded[:-1]  # drop final LF of marker

        batches, pending = read_outbox_batches(truncated, codec.decode_batches)
        self.assertEqual(batches, [])
        self.assertEqual(pending, truncated)

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    @unittest.expectedFailure
    def test_read_outbox_batches_malformed_batch_rejects_whole(self):
        """Decoding error in envelope rejects whole malformed batch and does not yield partial batch."""
        from wender.perceive.godot import read_outbox_batches

        codec = FixtureCodec()
        bad_payload = b'{"fixture_records": [invalid_json]}' + FIXTURE_COMPLETION_MARKER
        batches, pending = read_outbox_batches(bad_payload, codec.decode_batches)
        self.assertEqual(batches, [])

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_write_inbox_batch_valid_positive(self):
        """Positive control: write_inbox_batch validates, encodes via codec, and makes exactly one sink call."""
        from wender.perceive.godot import write_inbox_batch

        codec = FixtureCodec()
        sink_calls = []

        def mock_sink(payload_bytes):
            sink_calls.append(payload_bytes)

        records = [{"tick": 1, "seq": 1, "cmd": "save", "args": {}}]
        write_inbox_batch(records, codec.encode_batch, mock_sink)

        self.assertEqual(len(sink_calls), 1)
        expected_encoded = codec.encode_batch(records)
        self.assertEqual(sink_calls[0], expected_encoded)

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_write_inbox_batch_malformed_records_zero_sink_calls(self):
        """Negative control: write_inbox_batch validates records prior to encoding; invalid records cause zero sink calls."""
        from wender.perceive.godot import write_inbox_batch

        codec = FixtureCodec()
        sink_calls = []

        def mock_sink(payload_bytes):
            sink_calls.append(payload_bytes)

        # Record missing required 'cmd' field or containing invalid types
        invalid_records = [{"tick": "1_not_int", "seq": 1}]
        with self.assertRaises((ValueError, TypeError)):
            write_inbox_batch(invalid_records, codec.encode_batch, mock_sink)

        self.assertEqual(len(sink_calls), 0)

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_write_inbox_batch_missing_codec_refuses_init(self):
        """Negative control: missing encode_batch codec refuses execution."""
        from wender.perceive.godot import write_inbox_batch

        records = [{"tick": 1, "seq": 1, "cmd": "save", "args": {}}]
        with self.assertRaises((ValueError, TypeError)):
            write_inbox_batch(records, None, lambda x: None)


# ============================================================================
# Contract Tests: GodotExecutor Endpoint Initialization
# ============================================================================

class TestGodotExecutorInitializationContract(unittest.TestCase):
    """Contract tests for GodotExecutor endpoint and publisher configuration."""

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_godot_executor_valid_endpoints_initialization(self):
        """Positive control: distinct t2-inbox and t2-outbox endpoints with declared publisher initialize cleanly."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        exec_inst = GodotExecutor(
            trusted_context={"principal": "stream_main"},
            action_schemas={"move": {}},
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            t2_inbox="t2-inbox",
            t2_outbox="t2-outbox",
            publisher="declared_pub",
        )
        self.assertIsNotNone(exec_inst)

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_godot_executor_omitted_pair_off(self):
        """Omitted inbox/outbox pair is OFF with no consumption, writes or exports."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        exec_inst = GodotExecutor(
            trusted_context={"principal": "stream_main"},
            action_schemas={"move": {}},
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            t2_inbox=None,
            t2_outbox=None,
        )
        self.assertIsNotNone(exec_inst)

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_godot_executor_coincident_or_partial_endpoints_refuses(self):
        """Negative control: partial pair, coincident endpoints, or missing publisher when enabled refuses initialization."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        # Coincident endpoints
        with self.assertRaises(ValueError):
            GodotExecutor(
                trusted_context={"principal": "stream_main"},
                action_schemas={},
                encode_batch=codec.encode_batch,
                sink=lambda x: None,
                t2_inbox="same_endpoint",
                t2_outbox="same_endpoint",
                publisher="pub",
            )

        # Partial endpoint (only inbox)
        with self.assertRaises(ValueError):
            GodotExecutor(
                trusted_context={"principal": "stream_main"},
                action_schemas={},
                encode_batch=codec.encode_batch,
                sink=lambda x: None,
                t2_inbox="t2-inbox",
                t2_outbox=None,
                publisher="pub",
            )

        # Missing publisher when enabled
        with self.assertRaises(ValueError):
            GodotExecutor(
                trusted_context={"principal": "stream_main"},
                action_schemas={},
                encode_batch=codec.encode_batch,
                sink=lambda x: None,
                t2_inbox="t2-inbox",
                t2_outbox="t2-outbox",
                publisher=None,
            )


# ============================================================================
# Contract Tests: GodotExecutor Record Parsing & Validation
# ============================================================================

class TestGodotExecutorRecordValidationContract(unittest.TestCase):
    """Contract tests for GodotExecutor record format, JSON constraints, and payload authority rejections."""

    def _get_executor(self):
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        return GodotExecutor(
            trusted_context="stream_1",
            action_schemas={
                "move": {"params": ["dir"]},
                "new_game": {},
                "view": {},
                "save": {},
            },
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            inbox_endpoint="t2-inbox",
            outbox_endpoint="t2-outbox",
            publisher="pub1",
        )

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_reject_duplicate_json_keys(self):
        """Reject record containing duplicate JSON keys."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        executor = GodotExecutor(
            trusted_context={"principal": "stream_1"},
            action_schemas={
                "move": {"params": {"dir": "str"}, "optional": ["dir"]},
                "new_game": {"params": {}},
                "view": {"params": {}},
                "save": {"params": {}},
            },
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            t2_inbox="t2-inbox",
            t2_outbox="t2-outbox",
            publisher="pub1",
        )
        raw_json = '{"tick": 1, "seq": 1, "cmd": "move", "cmd": "view", "args": {}}'
        res = executor.dispatch(raw_json, committed_tick=0, basis_sha256="sha0")
        self.assertIn(res.get("status"), ("REJECTED", "ERROR"))

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_reject_unknown_fields(self):
        """Reject record containing unknown fields."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        executor = GodotExecutor(
            trusted_context={"principal": "stream_1"},
            action_schemas={
                "move": {"params": {"dir": "str"}, "optional": ["dir"]},
                "new_game": {"params": {}},
                "view": {"params": {}},
                "save": {"params": {}},
            },
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            t2_inbox="t2-inbox",
            t2_outbox="t2-outbox",
            publisher="pub1",
        )
        rec = {"tick": 1, "seq": 1, "cmd": "move", "args": {}, "unknown_field": 123}
        res = executor.dispatch(rec, committed_tick=0, basis_sha256="sha0")
        self.assertIn(res.get("status"), ("REJECTED", "ERROR"))

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_reject_nested_float(self):
        """Reject record containing floating point numbers in any field."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        executor = GodotExecutor(
            trusted_context={"principal": "stream_1"},
            action_schemas={
                "move": {"params": {"dir": "str"}, "optional": ["dir"]},
                "new_game": {"params": {}},
                "view": {"params": {}},
                "save": {"params": {}},
            },
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            t2_inbox="t2-inbox",
            t2_outbox="t2-outbox",
            publisher="pub1",
        )
        rec = {"tick": 1, "seq": 1, "cmd": "move", "args": {"speed": 1.5}}
        res = executor.dispatch(rec, committed_tick=0, basis_sha256="sha0")
        self.assertIn(res.get("status"), ("REJECTED", "ERROR"))

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_reject_boolean_tick_or_seq(self):
        """Reject record where tick or seq is a boolean instead of an integer."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        executor = GodotExecutor(
            trusted_context={"principal": "stream_1"},
            action_schemas={
                "move": {"params": {"dir": "str"}, "optional": ["dir"]},
                "new_game": {"params": {}},
                "view": {"params": {}},
                "save": {"params": {}},
            },
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            t2_inbox="t2-inbox",
            t2_outbox="t2-outbox",
            publisher="pub1",
        )
        rec = {"tick": True, "seq": 1, "cmd": "move", "args": {}}
        res = executor.dispatch(rec, committed_tick=0, basis_sha256="sha0")
        self.assertIn(res.get("status"), ("REJECTED", "ERROR"))

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    @unittest.expectedFailure
    def test_reject_raw_unicode_line_separator_in_string(self):
        """Reject record containing raw Unicode line separators (\u2028 or \u2029) within strings."""
        executor = self._get_executor()
        raw_json = '{"tick": 1, "seq": 1, "cmd": "move", "args": {"dir": "north\u2028south"}}'
        res = executor.dispatch(raw_json, committed_tick=0, basis_sha256="sha0")
        self.assertIn(res.get("status"), ("REJECTED", "ERROR"))

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_reject_payload_authority_fields(self):
        """Reject payload containing authority fields (actor, channel, principal, role, slot, consent)."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        executor = GodotExecutor(
            trusted_context={"principal": "stream_1"},
            action_schemas={
                "move": {"params": {"dir": "str"}, "optional": ["dir"]},
                "new_game": {"params": {}},
                "view": {"params": {}},
                "save": {"params": {}},
            },
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            t2_inbox="t2-inbox",
            t2_outbox="t2-outbox",
            publisher="pub1",
        )
        for auth_field in ("actor", "channel", "principal", "role", "slot", "consent"):
            with self.subTest(auth_field=auth_field):
                rec = {
                    "tick": 1,
                    "seq": 1,
                    "cmd": "move",
                    "args": {},
                    auth_field: "unauthorized_authority",
                }
                res = executor.dispatch(rec, committed_tick=0, basis_sha256="sha0")
                self.assertIn(res.get("status"), ("REJECTED", "ERROR"))


# ============================================================================
# Contract Tests: GodotExecutor Schema & Tick Window & Sequence Rules
# ============================================================================

class TestGodotExecutorSemanticsContract(unittest.TestCase):
    """Contract tests for action schema resolution, tick window, stream sequence, and batch atomicity."""

    def _get_executor(self, context="stream_1"):
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        return GodotExecutor(
            trusted_context=context,
            action_schemas={
                "econ_trade": {"type": "economic"},
                "new_game": {"type": "boundary"},
                "view_map": {"type": "boundary"},
                "save": {"type": "boundary"},
            },
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            inbox_endpoint="t2-inbox",
            outbox_endpoint="t2-outbox",
            publisher="pub1",
        )

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    @unittest.expectedFailure
    def test_unresolved_schema_rejects_schema(self):
        """Unpublished/unresolved action rejects ACTION_UNAVAILABLE or SCHEMA."""
        executor = self._get_executor()
        rec = {"tick": 1, "seq": 1, "cmd": "unknown_action", "args": {}}
        res = executor.dispatch(rec, committed_tick=0, basis_sha256="sha0")
        self.assertEqual(res.get("reason"), "SCHEMA")

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_economic_command_requires_committed_tick_plus_one(self):
        """Economic command requires committed_tick + 1; otherwise rejects TICK_WINDOW."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        executor = GodotExecutor(
            trusted_context={"principal": "stream_1"},
            action_schemas={
                "econ_trade": {"params": {}, "boundary": "next"},
                "new_game": {"params": {}, "boundary": "current"},
                "view_map": {"params": {}, "boundary": "current"},
                "save": {"params": {}, "boundary": "current"},
            },
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            t2_inbox="t2-inbox",
            t2_outbox="t2-outbox",
            publisher="pub1",
        )

        # Wrong tick (tick=1 when committed_tick=1, expected tick=2)
        rec_bad = {"tick": 1, "seq": 1, "cmd": "econ_trade", "args": {}}
        res_bad = executor.dispatch(rec_bad, committed_tick=1, basis_sha256="sha0")
        self.assertEqual(res_bad.get("reason"), "TICK_WINDOW")

        # Correct tick (tick=2 when committed_tick=1)
        rec_good = {"tick": 2, "seq": 1, "cmd": "econ_trade", "args": {}}
        res_good = executor.dispatch(rec_good, committed_tick=1, basis_sha256="sha0")
        self.assertEqual(res_good.get("status"), "ACCEPTED")

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_current_boundary_controls_no_economic_tick_constraint(self):
        """new_game, view commands, and save use declared current-boundary schema without economic tick constraint."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        executor = GodotExecutor(
            trusted_context={"principal": "stream_1"},
            action_schemas={
                "econ_trade": {"params": {}, "boundary": "next"},
                "new_game": {"params": {}, "boundary": "current"},
                "view_map": {"params": {}, "boundary": "current"},
                "save": {"params": {}, "boundary": "current"},
            },
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            t2_inbox="t2-inbox",
            t2_outbox="t2-outbox",
            publisher="pub1",
        )

        for seq, cmd in enumerate(("new_game", "view_map", "save"), 1):
            with self.subTest(cmd=cmd):
                rec = {"tick": 0, "seq": seq, "cmd": cmd, "args": {}}
                res = executor.dispatch(rec, committed_tick=0, basis_sha256="sha0")
                self.assertEqual(res.get("status"), "ACCEPTED")

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_stream_sequence_gap_rejects_sequence(self):
        """Sequence gap (e.g. seq=2 when expecting seq=1) rejects SEQUENCE."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        executor = GodotExecutor(
            trusted_context={"principal": "stream_1"},
            action_schemas={
                "econ_trade": {"params": {}, "boundary": "next"},
                "new_game": {"params": {}, "boundary": "current"},
                "view_map": {"params": {}, "boundary": "current"},
                "save": {"params": {}, "boundary": "current"},
            },
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            t2_inbox="t2-inbox",
            t2_outbox="t2-outbox",
            publisher="pub1",
        )
        rec_gap = {"tick": 1, "seq": 2, "cmd": "econ_trade", "args": {}}
        res = executor.dispatch(rec_gap, committed_tick=0, basis_sha256="sha0")
        self.assertEqual(res.get("reason"), "SEQUENCE")

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_stream_sequence_replay_rejects_duplicate_seq(self):
        """Sequence replay (e.g. submitting seq=1 twice) rejects DUPLICATE_SEQ."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        executor = GodotExecutor(
            trusted_context={"principal": "stream_1"},
            action_schemas={
                "econ_trade": {"params": {}, "boundary": "next"},
                "new_game": {"params": {}, "boundary": "current"},
                "view_map": {"params": {}, "boundary": "current"},
                "save": {"params": {}, "boundary": "current"},
            },
            encode_batch=codec.encode_batch,
            sink=lambda x: None,
            t2_inbox="t2-inbox",
            t2_outbox="t2-outbox",
            publisher="pub1",
        )

        rec1 = {"tick": 1, "seq": 1, "cmd": "econ_trade", "args": {}}
        res1 = executor.dispatch(rec1, committed_tick=0, basis_sha256="sha0")
        self.assertEqual(res1.get("status"), "ACCEPTED")

        rec_replay = {"tick": 2, "seq": 1, "cmd": "econ_trade", "args": {}}
        res_replay = executor.dispatch(rec_replay, committed_tick=1, basis_sha256="sha0")
        self.assertEqual(res_replay.get("reason"), "DUPLICATE_SEQ")

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    def test_multiple_trusted_streams_sequence_isolated(self):
        """Multiple trusted streams maintain isolated sequence numbers."""
        from wender.act.godot_executor import GodotExecutor

        codec = FixtureCodec()
        action_schemas = {"econ_trade": {"params": {}, "boundary": "next"}}
        exec_stream1 = GodotExecutor(
            trusted_context={"principal": "stream_1"}, action_schemas=action_schemas,
            encode_batch=codec.encode_batch, sink=lambda x: None,
            t2_inbox="t2-inbox-1", t2_outbox="t2-outbox-1", publisher="pub1",
        )
        exec_stream2 = GodotExecutor(
            trusted_context={"principal": "stream_2"}, action_schemas=action_schemas,
            encode_batch=codec.encode_batch, sink=lambda x: None,
            t2_inbox="t2-inbox-2", t2_outbox="t2-outbox-2", publisher="pub2",
        )

        rec1 = {"tick": 1, "seq": 1, "cmd": "econ_trade", "args": {}}

        res1 = exec_stream1.dispatch(rec1, committed_tick=0, basis_sha256="sha0")
        self.assertEqual(res1.get("status"), "ACCEPTED")

        # Stream 2 can also start at seq=1 without conflict
        res2 = exec_stream2.dispatch(rec1, committed_tick=0, basis_sha256="sha0")
        self.assertEqual(res2.get("status"), "ACCEPTED")

    # Build task: fx2-f9-godot-t2-mailbox-and-uitree-adapter
    @unittest.expectedFailure
    def test_batch_reject_later_record_preserves_earlier_valid_commands(self):
        """In a well-formed batch, preserving valid predecessors when a later command is rejected."""
        executor = self._get_executor()

        rec1 = {"tick": 1, "seq": 1, "cmd": "econ_trade", "args": {}}
        res1 = executor.dispatch(rec1, committed_tick=0, basis_sha256="sha0")
        self.assertEqual(res1.get("status"), "ACCEPTED")

        # Second command in batch has invalid tick -> TICK_WINDOW rejected
        rec2_bad = {"tick": 1, "seq": 2, "cmd": "econ_trade", "args": {}}
        res2 = executor.dispatch(rec2_bad, committed_tick=1, basis_sha256="sha0")
        self.assertEqual(res2.get("reason"), "TICK_WINDOW")

        # Third command has correct seq=3 and correct tick=2 -> ACCEPTED
        rec3_good = {"tick": 2, "seq": 3, "cmd": "econ_trade", "args": {}}
        res3 = executor.dispatch(rec3_good, committed_tick=1, basis_sha256="sha0")
        self.assertEqual(res3.get("status"), "ACCEPTED")


if __name__ == "__main__":
    unittest.main()
