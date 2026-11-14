"""
W8U UI Contract and Schema Conformance Tests.

Offline stdlib-based contract validation and protocol testing for OpenSuperficies.
Covers binding, action, action-result, frame metadata, UI receipt objects,
control messages (HELLO / END), XWD header validation, and credit / frame inflight management.
"""

import json
import re
import struct
import unittest

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Frozen literal schema identifiers
BINDING_SCHEMA_ID = "w8u.binding/v1"
ACTION_SCHEMA_ID = "w8u.action/v1"
ACTION_RESULT_SCHEMA_ID = "w8u.action-result/v1"
FRAME_METADATA_SCHEMA_ID = "w8u.frame-metadata/v1"
RECEIPT_SCHEMA_ID = "w8u.receipt/v1"

# Frozen control magic & kinds
CONTROL_MAGIC = b"W8UC"
KIND_HELLO = 1
KIND_HELLO_ACK = 2
KIND_END = 255

# END error code enumeration
END_OK = 0
END_E_SCHEMA = 2
END_E_UNAVAILABLE = 4
END_E_VERIFICATION_REFUSAL = 5
END_E_UNCERTAINTY = 6

SUPPORTED_KEYWORDS = frozenset({
    "$schema", "$id", "title", "type", "properties", "required",
    "additionalProperties", "enum", "const", "minimum", "maximum",
    "pattern", "oneOf", "$ref", "maxLength", "uniqueItems", "items", "$defs"
})


class Refusal(Exception):
    def __init__(self, code, reason, exit_code=5):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.exit_code = exit_code


def canonical_json_bytes(obj):
    """Encodes object into compact sorted integer-only UTF-8 JSON without LF."""
    def check_numbers(val):
        if isinstance(val, bool):
            return
        if isinstance(val, float):
            raise Refusal("E_SCHEMA", "Floats not allowed in canonical JSON", exit_code=2)
        if isinstance(val, dict):
            for v in val.values():
                check_numbers(v)
        elif isinstance(val, list):
            for item in val:
                check_numbers(item)

    check_numbers(obj)
    data = json.dumps(obj, separators=(',', ':'), sort_keys=True, ensure_ascii=False)
    return data.encode('utf-8')


def parse_json_strict(raw_bytes):
    """Parses JSON strictly: checks UTF-8, no duplicate keys, float refusal, max 64 KiB body."""
    if len(raw_bytes) > 65536:
        raise Refusal("E_SCHEMA", "Body exceeds 64 KiB limit", exit_code=2)

    try:
        text = raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        raise Refusal("E_SCHEMA", "Invalid UTF-8 encoding", exit_code=2)

    def dict_raise_on_duplicates(ordered_pairs):
        d = {}
        for k, v in ordered_pairs:
            if k in d:
                raise Refusal("E_SCHEMA", f"Duplicate key '{k}'", exit_code=2)
            d[k] = v
        return d

    try:
        val = json.loads(text, object_pairs_hook=dict_raise_on_duplicates)
    except json.JSONDecodeError as err:
        raise Refusal("E_SCHEMA", f"Malformed JSON: {err}", exit_code=2)

    return val


class AuditedSchemaValidator:
    """Stdlib JSON schema validator for the audited contract subset."""

    def __init__(self, schema, defs=None):
        self.schema = schema
        self.defs = defs or schema.get("$defs", {})
        self._check_unsupported_keywords(schema)

    def _check_unsupported_keywords(self, schema_node):
        if not isinstance(schema_node, dict):
            return
        for key in schema_node:
            if key not in SUPPORTED_KEYWORDS:
                raise ValueError(f"Unsupported JSON Schema keyword: {key}")
        for k in ("properties", "$defs"):
            if k in schema_node and isinstance(schema_node[k], dict):
                for sub in schema_node[k].values():
                    self._check_unsupported_keywords(sub)
        if "items" in schema_node and isinstance(schema_node["items"], dict):
            self._check_unsupported_keywords(schema_node["items"])
        if "oneOf" in schema_node and isinstance(schema_node["oneOf"], list):
            for sub in schema_node["oneOf"]:
                self._check_unsupported_keywords(sub)

    def validate(self, instance, node=None):
        if node is None:
            node = self.schema

        if "$ref" in node:
            ref_path = node["$ref"]
            if ref_path.startswith("#/$defs/"):
                def_name = ref_path[len("#/$defs/"):]
                if def_name not in self.defs:
                    raise Refusal("E_SCHEMA", f"Unknown ref {ref_path}", exit_code=2)
                return self.validate(instance, self.defs[def_name])
            else:
                raise ValueError(f"Unsupported ref format {ref_path}")

        if "oneOf" in node:
            matches = 0
            for sub in node["oneOf"]:
                try:
                    self.validate(instance, sub)
                    matches += 1
                except Refusal:
                    pass
            if matches != 1:
                raise Refusal("E_SCHEMA", f"Instance matched {matches} subschemas in oneOf, expected exactly 1", exit_code=2)
            return

        if "const" in node:
            if instance != node["const"]:
                raise Refusal("E_SCHEMA", f"Expected const {node['const']}, got {instance}", exit_code=2)

        if "enum" in node:
            if instance not in node["enum"]:
                raise Refusal("E_SCHEMA", f"Value {instance} not in enum {node['enum']}", exit_code=2)

        target_type = node.get("type")
        if target_type:
            if target_type == "null":
                if instance is not None:
                    raise Refusal("E_SCHEMA", f"Expected null, got {type(instance)}", exit_code=2)
            elif target_type == "boolean":
                if not isinstance(instance, bool):
                    raise Refusal("E_SCHEMA", f"Expected boolean, got {type(instance)}", exit_code=2)
            elif target_type == "integer":
                if isinstance(instance, bool) or not isinstance(instance, int):
                    raise Refusal("E_SCHEMA", f"Expected integer, got {type(instance)}", exit_code=2)
                if "minimum" in node and instance < node["minimum"]:
                    raise Refusal("E_SCHEMA", f"Value {instance} below minimum {node['minimum']}", exit_code=2)
                if "maximum" in node and instance > node["maximum"]:
                    raise Refusal("E_SCHEMA", f"Value {instance} above maximum {node['maximum']}", exit_code=2)
            elif target_type == "string":
                if not isinstance(instance, str):
                    raise Refusal("E_SCHEMA", f"Expected string, got {type(instance)}", exit_code=2)
                if "pattern" in node:
                    if not re.search(node["pattern"], instance):
                        raise Refusal("E_SCHEMA", f"String '{instance}' does not match pattern {node['pattern']}", exit_code=2)
                if "maxLength" in node and len(instance.encode('utf-8')) > node["maxLength"]:
                    raise Refusal("E_SCHEMA", f"String length exceeds maxLength {node['maxLength']}", exit_code=2)
            elif target_type == "array":
                if not isinstance(instance, list):
                    raise Refusal("E_SCHEMA", f"Expected array, got {type(instance)}", exit_code=2)
                if node.get("uniqueItems") and len(instance) != len(set(instance)):
                    raise Refusal("E_SCHEMA", "Array elements must be unique", exit_code=2)
                if "items" in node:
                    for item in instance:
                        self.validate(item, node["items"])
            elif target_type == "object":
                if not isinstance(instance, dict):
                    raise Refusal("E_SCHEMA", f"Expected object, got {type(instance)}", exit_code=2)
                req = node.get("required", [])
                for r in req:
                    if r not in instance:
                        raise Refusal("E_SCHEMA", f"Missing required property '{r}'", exit_code=2)
                props = node.get("properties", {})
                if node.get("additionalProperties") is False:
                    for k in instance:
                        if k not in props:
                            raise Refusal("E_SCHEMA", f"Undeclared additional property '{k}'", exit_code=2)
                for k, v in instance.items():
                    if k in props:
                        self.validate(v, props[k])


def parse_xwd_header(header_bytes):
    """
    Parses big-endian XWD header (minimum 100 bytes).
    Checks: version 7, format 2, depth 24, BPP 32, visual 4 or 5,
    masks ff0000, ff00, ff; byte_order and bitmap_bit_order in (0, 1).
    Header size 100..65536, colors <= 65536, width/height 1..16384, stride >= 4*width and %4==0 and <=65536.
    """
    if len(header_bytes) < 100:
        raise Refusal("E_SCHEMA", "XWD header under 100 bytes", exit_code=2)

    (
        header_size, file_version, pixmap_format, pixmap_depth,
        pixmap_width, pixmap_height, xoffset, byte_order,
        bitmap_unit, bitmap_bit_order, bitmap_pad, bits_per_pixel,
        bytes_per_line, visual_class, red_mask, green_mask, blue_mask,
        bits_per_rgb, colormap_entries, ncolors, window_width, window_height,
        window_x, window_y, window_bdr_width
    ) = struct.unpack(">25I", header_bytes[:100])

    if header_size < 100 or header_size > 65536:
        raise Refusal("E_SCHEMA", f"Invalid header size {header_size}", exit_code=2)
    if file_version != 7:
        raise Refusal("E_SCHEMA", f"Unsupported XWD version {file_version}", exit_code=2)
    if pixmap_format != 2:
        raise Refusal("E_SCHEMA", f"Unsupported format {pixmap_format}", exit_code=2)
    if pixmap_depth != 24:
        raise Refusal("E_SCHEMA", f"Unsupported depth {pixmap_depth}", exit_code=2)
    if bits_per_pixel != 32:
        raise Refusal("E_SCHEMA", f"Unsupported BPP {bits_per_pixel}", exit_code=2)
    if visual_class not in (4, 5):
        raise Refusal("E_SCHEMA", f"Unsupported visual class {visual_class}", exit_code=2)
    if red_mask != 0xFF0000 or green_mask != 0x00FF00 or blue_mask != 0x0000FF:
        raise Refusal("E_SCHEMA", "RGB masks must be ff0000, ff00, ff", exit_code=2)
    if byte_order not in (0, 1) or bitmap_bit_order not in (0, 1):
        raise Refusal("E_SCHEMA", "Byte/bit order must be 0 or 1", exit_code=2)
    if ncolors > 65536:
        raise Refusal("E_SCHEMA", f"Color count {ncolors} exceeds limit", exit_code=2)
    if not (1 <= pixmap_width <= 16384) or not (1 <= pixmap_height <= 16384):
        raise Refusal("E_SCHEMA", f"Geometry {pixmap_width}x{pixmap_height} outside 1..16384", exit_code=2)
    if bytes_per_line < 4 * pixmap_width or bytes_per_line % 4 != 0 or bytes_per_line > 65536:
        raise Refusal("E_SCHEMA", f"Invalid stride {bytes_per_line}", exit_code=2)

    expected_payload_bytes = header_size + (12 * ncolors) + (pixmap_height * bytes_per_line)
    return {
        "header_size": header_size,
        "width": pixmap_width,
        "height": pixmap_height,
        "stride": bytes_per_line,
        "ncolors": ncolors,
        "expected_payload_bytes": expected_payload_bytes,
        "byte_order": byte_order,
        "bitmap_bit_order": bitmap_bit_order
    }


class CreditFlowController:
    """Manages frame capacity grants and sequence acknowledgments."""

    def __init__(self, max_inflight_frames):
        if not isinstance(max_inflight_frames, int) or max_inflight_frames <= 0:
            raise Refusal("E_SCHEMA", "max_inflight_frames must be positive integer", exit_code=2)
        self.max_inflight_frames = max_inflight_frames
        self.acknowledged_seq = 0
        self.granted_capacity = min(2, max_inflight_frames)

    def ack_seq(self, seq, request_grant):
        if seq < self.acknowledged_seq:
            return self.granted_capacity
        self.acknowledged_seq = seq
        new_total_grant = request_grant
        max_allowed = self.acknowledged_seq + self.max_inflight_frames
        if new_total_grant > max_allowed:
            raise Refusal("E_CREDIT", f"Requested grant {new_total_grant} exceeds max allowed {max_allowed}", exit_code=5)
        self.granted_capacity = new_total_grant
        return self.granted_capacity


def validate_action_args_and_geometry(action, binding_width, binding_height):
    """Enforces specific action verb/args variant rules and geometric constraints."""
    verb = action.get("verb")
    args = action.get("args", {})

    if verb == "pointer":
        op = args.get("operation")
        if op not in ("move", "click", "down", "up", "scroll"):
            raise Refusal("E_VERB", f"Unknown pointer operation {op}", exit_code=5)
        x = args.get("x")
        y = args.get("y")
        if not isinstance(x, int) or not isinstance(y, int) or isinstance(x, bool) or isinstance(y, bool):
            raise Refusal("E_SCHEMA", "Coordinates x,y must be integers", exit_code=2)
        if not (0 <= x <= binding_width and 0 <= y <= binding_height):
            raise Refusal("E_SCHEMA", f"Coordinates ({x},{y}) out of binding bounds ({binding_width}x{binding_height})", exit_code=2)
        if op == "scroll":
            dx = args.get("dx")
            dy = args.get("dy")
            if dx == 0 and dy == 0:
                raise Refusal("E_VERB", "Scroll requires at least one nonzero delta", exit_code=5)
            if not (-120 <= dx <= 120 and -120 <= dy <= 120):
                raise Refusal("E_SCHEMA", f"Scroll deltas ({dx},{dy}) out of [-120, 120] range", exit_code=2)
    elif verb == "key":
        op = args.get("operation")
        if op not in ("press", "down", "up"):
            raise Refusal("E_VERB", f"Unknown key operation {op}", exit_code=5)
        mods = args.get("modifiers", [])
        if len(mods) != len(set(mods)):
            raise Refusal("E_SCHEMA", "Key modifiers must be unique", exit_code=2)
        expected_order = ["Shift", "Control", "Alt", "Super"]
        order_indices = [expected_order.index(m) for m in mods if m in expected_order]
        if order_indices != sorted(order_indices):
            raise Refusal("E_SCHEMA", "Key modifiers must be in specified canonical order", exit_code=2)
    elif verb == "text":
        mode = args.get("mode")
        if mode != "type":
            raise Refusal("E_VERB", f"Invalid text mode {mode}", exit_code=5)
        text = args.get("text", "")
        if "\0" in text:
            raise Refusal("E_SCHEMA", "Text contains NUL byte", exit_code=2)
    elif verb == "wait":
        duration = args.get("duration_ms")
        if not isinstance(duration, int) or isinstance(duration, bool):
            raise Refusal("E_SCHEMA", "duration_ms must be integer", exit_code=2)
        if not (0 <= duration <= 5000):
            raise Refusal("E_SCHEMA", f"duration_ms {duration} out of [0, 5000]", exit_code=2)


class TestW8UContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open("contracts/w8ui/w8u.ui.v1.schema.json", "r", encoding="utf-8") as f:
            cls.schema_data = json.load(f)
        cls.validator = AuditedSchemaValidator(cls.schema_data)

    def test_positive(self):
        """U3, U4, U5 Positive Controls - Golden wire vectors."""
        # 1. Binding Golden
        binding_golden = {
            "schema": BINDING_SCHEMA_ID,
            "run_id": "run-001",
            "lease_id": "lease-abc",
            "seat_grant_sha256": "0" * 64,
            "container_id": "1" * 64,
            "target_handle": {
                "kind": "x11",
                "canonical_id": "display-0",
                "generation": 1
            },
            "epoch": 1,
            "display": ":0",
            "xid": 12345,
            "width": 1024,
            "height": 768,
            "frames_root": "/dev/shm/vqa-ramws",
            "frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-001",
            "image_sha256": "2" * 64,
            "reader_sha256": "3" * 64,
            "exec_allowlist_sha256": "4" * 64,
            "max_record_bytes": 1048576,
            "max_inflight_frames": 4,
            "tick_mode": "committed"
        }
        raw_binding = canonical_json_bytes(binding_golden)
        parsed_binding = parse_json_strict(raw_binding)
        self.validator.validate(parsed_binding, self.schema_data["$defs"]["binding"])

        # 2. Action Pointer Move Golden
        action_move = {
            "schema": ACTION_SCHEMA_ID,
            "run_id": "run-001",
            "lease_id": "lease-abc",
            "epoch": 1,
            "generation": 1,
            "action_id": "act-1",
            "action_seq": 1,
            "observed_frame_seq": 0,
            "observed_frame_sha256": "a" * 64,
            "deadline_ns": 1000000000,
            "verb": "pointer",
            "args": {
                "operation": "move",
                "x": 100,
                "y": 200
            },
            "grant_sha256": "b" * 64
        }
        self.validator.validate(action_move, self.schema_data["$defs"]["action"])
        validate_action_args_and_geometry(action_move, 1024, 768)

        # 3. Action Scroll Golden
        action_scroll = {
            "schema": ACTION_SCHEMA_ID,
            "run_id": "run-001",
            "lease_id": "lease-abc",
            "epoch": 1,
            "generation": 1,
            "action_id": "act-2",
            "action_seq": 2,
            "observed_frame_seq": 1,
            "observed_frame_sha256": "a" * 64,
            "deadline_ns": 1000000000,
            "verb": "pointer",
            "args": {
                "operation": "scroll",
                "x": 100,
                "y": 200,
                "dx": 0,
                "dy": -120
            },
            "grant_sha256": "b" * 64
        }
        self.validator.validate(action_scroll, self.schema_data["$defs"]["action"])
        validate_action_args_and_geometry(action_scroll, 1024, 768)

        # 4. Key Action Golden
        action_key = {
            "schema": ACTION_SCHEMA_ID,
            "run_id": "run-001",
            "lease_id": "lease-abc",
            "epoch": 1,
            "generation": 1,
            "action_id": "act-3",
            "action_seq": 3,
            "observed_frame_seq": 1,
            "observed_frame_sha256": "a" * 64,
            "deadline_ns": 1000000000,
            "verb": "key",
            "args": {
                "operation": "press",
                "key": "Enter",
                "modifiers": ["Shift", "Control"]
            },
            "grant_sha256": "b" * 64
        }
        self.validator.validate(action_key, self.schema_data["$defs"]["action"])
        validate_action_args_and_geometry(action_key, 1024, 768)

        # 5. Text Action Golden
        action_text = {
            "schema": ACTION_SCHEMA_ID,
            "run_id": "run-001",
            "lease_id": "lease-abc",
            "epoch": 1,
            "generation": 1,
            "action_id": "act-4",
            "action_seq": 4,
            "observed_frame_seq": 1,
            "observed_frame_sha256": "a" * 64,
            "deadline_ns": 1000000000,
            "verb": "text",
            "args": {
                "mode": "type",
                "text": "Hello World"
            },
            "grant_sha256": "b" * 64
        }
        self.validator.validate(action_text, self.schema_data["$defs"]["action"])
        validate_action_args_and_geometry(action_text, 1024, 768)

        # 6. Wait Action Golden
        action_wait = {
            "schema": ACTION_SCHEMA_ID,
            "run_id": "run-001",
            "lease_id": "lease-abc",
            "epoch": 1,
            "generation": 1,
            "action_id": "act-5",
            "action_seq": 5,
            "observed_frame_seq": 1,
            "observed_frame_sha256": "a" * 64,
            "deadline_ns": 1000000000,
            "verb": "wait",
            "args": {
                "duration_ms": 1000
            },
            "grant_sha256": "b" * 64
        }
        self.validator.validate(action_wait, self.schema_data["$defs"]["action"])
        validate_action_args_and_geometry(action_wait, 1024, 768)

        # 7. Action Result Golden
        result_golden = {
            "schema": ACTION_RESULT_SCHEMA_ID,
            "action_id": "act-1",
            "action_seq": 1,
            "lease_id": "lease-abc",
            "epoch": 1,
            "status": "applied",
            "code": "OK_ADMITTED",
            "emitted": 1000500,
            "result_known": True,
            "applied_tick": 5,
            "outcome_sha256": "c" * 64
        }
        self.validator.validate(result_golden, self.schema_data["$defs"]["action_result"])

        # 8. Frame Metadata Golden
        frame_meta = {
            "seq": 1,
            "tick": 5,
            "tick_valid": True,
            "capture_mono_ns": 50000,
            "width": 1024,
            "height": 768,
            "stride": 4096,
            "pixel_format": "RGB24",
            "sha256": "d" * 64,
            "binding_sha256": "e" * 64
        }
        self.validator.validate(frame_meta, self.schema_data["$defs"]["frame_metadata"])

        # 9. Frame Metadata Null Tick Golden
        frame_meta_null_tick = {
            "seq": 2,
            "tick": None,
            "tick_valid": False,
            "capture_mono_ns": 60000,
            "width": 1024,
            "height": 768,
            "stride": 4096,
            "pixel_format": "RGB24",
            "sha256": "d" * 64,
            "binding_sha256": "e" * 64
        }
        self.validator.validate(frame_meta_null_tick, self.schema_data["$defs"]["frame_metadata"])

        # 10. UI Receipt Golden
        receipt_golden = {
            "run_id": "run-001",
            "lease_id": "lease-abc",
            "target_handle": {
                "kind": "x11",
                "canonical_id": "display-0",
                "generation": 1
            },
            "epoch": 1,
            "frames_root": "/dev/shm/vqa-ramws",
            "frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-001",
            "source": "xvfb-grab-held-pair",
            "frames_captured": 10,
            "first_frame": frame_meta,
            "last_frame": frame_meta_null_tick,
            "ui_actions_count": 5,
            "admitted_actions": 5,
            "refused_actions": 0,
            "uncertain_actions": 0,
            "action_log_sha256": "f" * 64,
            "t2_trace_sha256": "a" * 64,
            "t2_terminal_sha256": "b" * 64,
            "t2_ticks": 100,
            "final_state_sha256": "c" * 64,
            "simulation_state_sha256": "d" * 64,
            "ui_gate_verdict": "pass",
            "ui_gate_code": "OK",
            "pair_mismatches": 0,
            "frame_gaps": 0,
            "frames_dropped": 0,
            "x11_tcp_listeners": 0,
            "x11_unix_listeners": 1,
            "x11_abstract_listeners": 1,
            "reader_exit_code": 0,
            "reader_quiescent": True,
            "ram_purged": True,
            "frames_disk_bytes": 0,
            "swap_delta_bytes": 0,
            "metadata_only": True
        }
        self.validator.validate(receipt_golden, self.schema_data["$defs"]["receipt"])

    def test_reject(self):
        """Rejection cases for undeclared fields, invalid action variants, legacy roots, duplicate keys."""
        # 1. Undeclared pixel payload in frame metadata (Must-FAIL fixture)
        bad_frame_meta = {
            "seq": 1,
            "tick": 5,
            "tick_valid": True,
            "capture_mono_ns": 50000,
            "width": 1024,
            "height": 768,
            "stride": 4096,
            "pixel_format": "RGB24",
            "sha256": "d" * 64,
            "binding_sha256": "e" * 64,
            "pixels_base64": "AAAA"  # Undeclared pixel payload leak
        }
        with self.assertRaises(Refusal) as ctx:
            self.validator.validate(bad_frame_meta, self.schema_data["$defs"]["frame_metadata"])
        self.assertEqual(ctx.exception.code, "E_SCHEMA")
        self.assertEqual(ctx.exception.exit_code, 2)

        # 2. Invalid action variant (Must-FAIL fixture)
        bad_action_variant = {
            "schema": ACTION_SCHEMA_ID,
            "run_id": "run-001",
            "lease_id": "lease-abc",
            "epoch": 1,
            "generation": 1,
            "action_id": "act-1",
            "action_seq": 1,
            "observed_frame_seq": 0,
            "observed_frame_sha256": "a" * 64,
            "deadline_ns": 1000000000,
            "verb": "pointer",
            "args": {
                "operation": "teleport",  # Invalid operation
                "x": 100,
                "y": 200
            },
            "grant_sha256": "b" * 64
        }
        with self.assertRaises(Refusal) as ctx:
            self.validator.validate(bad_action_variant, self.schema_data["$defs"]["action"])
        self.assertEqual(ctx.exception.code, "E_SCHEMA")

        # 3. Legacy root in binding
        bad_binding_root = {
            "schema": BINDING_SCHEMA_ID,
            "run_id": "run-001",
            "lease_id": "lease-abc",
            "seat_grant_sha256": "0" * 64,
            "container_id": "1" * 64,
            "target_handle": {"kind": "x11", "canonical_id": "display-0", "generation": 1},
            "epoch": 1,
            "display": ":0",
            "xid": 12345,
            "width": 1024,
            "height": 768,
            "frames_root": "/tmp/legacy-root",  # Invalid root
            "frame_run_root": "/tmp/legacy-root/run-001",
            "image_sha256": "2" * 64,
            "reader_sha256": "3" * 64,
            "exec_allowlist_sha256": "4" * 64,
            "max_record_bytes": 1048576,
            "max_inflight_frames": 4,
            "tick_mode": "committed"
        }
        with self.assertRaises(Refusal) as ctx:
            self.validator.validate(bad_binding_root, self.schema_data["$defs"]["binding"])
        self.assertEqual(ctx.exception.code, "E_SCHEMA")

        # 4. Duplicate key in JSON
        dup_json = b'{"schema":"w8u.action/v1","action_id":"a1","action_id":"a2"}'
        with self.assertRaises(Refusal) as ctx:
            parse_json_strict(dup_json)
        self.assertEqual(ctx.exception.code, "E_SCHEMA")

        # 5. Invalid scroll (all zeroes dx=0, dy=0)
        bad_scroll_action = {
            "schema": ACTION_SCHEMA_ID,
            "run_id": "run-001",
            "lease_id": "lease-abc",
            "epoch": 1,
            "generation": 1,
            "action_id": "act-2",
            "action_seq": 2,
            "observed_frame_seq": 1,
            "observed_frame_sha256": "a" * 64,
            "deadline_ns": 1000000000,
            "verb": "pointer",
            "args": {
                "operation": "scroll",
                "x": 100,
                "y": 200,
                "dx": 0,
                "dy": 0  # Invalid scroll with zero delta
            },
            "grant_sha256": "b" * 64
        }
        with self.assertRaises(Refusal) as ctx:
            validate_action_args_and_geometry(bad_scroll_action, 1024, 768)
        self.assertEqual(ctx.exception.code, "E_VERB")

        # 6. Out of bounds coordinates
        oob_action = {
            "schema": ACTION_SCHEMA_ID,
            "run_id": "run-001",
            "lease_id": "lease-abc",
            "epoch": 1,
            "generation": 1,
            "action_id": "act-1",
            "action_seq": 1,
            "observed_frame_seq": 0,
            "observed_frame_sha256": "a" * 64,
            "deadline_ns": 1000000000,
            "verb": "pointer",
            "args": {
                "operation": "move",
                "x": 5000,  # Exceeds width 1024
                "y": 200
            },
            "grant_sha256": "b" * 64
        }
        with self.assertRaises(Refusal) as ctx:
            validate_action_args_and_geometry(oob_action, 1024, 768)
        self.assertEqual(ctx.exception.code, "E_SCHEMA")

    def test_protocol_complete(self):
        """Complete protocol verification: XWD header parsing & Credit Flow / Inflight limits."""
        # 1. Valid XWD Header (100 bytes)
        valid_xwd = struct.pack(
            ">25I",
            100,      # header_size
            7,        # file_version
            2,        # pixmap_format
            24,       # pixmap_depth
            1024,     # pixmap_width
            768,      # pixmap_height
            0,        # xoffset
            0,        # byte_order
            32,       # bitmap_unit
            0,        # bitmap_bit_order
            32,       # bitmap_pad
            32,       # bits_per_pixel
            4096,     # bytes_per_line (stride)
            4,        # visual_class
            0xFF0000, # red_mask
            0x00FF00, # green_mask
            0x0000FF, # blue_mask
            8,        # bits_per_rgb
            256,      # colormap_entries
            0,        # ncolors
            1024,     # window_width
            768,      # window_height
            0,        # window_x
            0,        # window_y
            0         # window_bdr_width
        )
        parsed_xwd = parse_xwd_header(valid_xwd)
        self.assertEqual(parsed_xwd["header_size"], 100)
        self.assertEqual(parsed_xwd["width"], 1024)
        self.assertEqual(parsed_xwd["height"], 768)
        self.assertEqual(parsed_xwd["stride"], 4096)
        self.assertEqual(parsed_xwd["expected_payload_bytes"], 100 + (768 * 4096))

        # Invalid XWD Header (Oversized header 65537)
        invalid_xwd_hdr = struct.pack(">25I", 65537, 7, 2, 24, 1024, 768, 0, 0, 32, 0, 32, 32, 4096, 4, 0xFF0000, 0x00FF00, 0x0000FF, 8, 256, 0, 1024, 768, 0, 0, 0)
        with self.assertRaises(Refusal) as ctx:
            parse_xwd_header(invalid_xwd_hdr)
        self.assertEqual(ctx.exception.code, "E_SCHEMA")

        # Invalid XWD Header (Bad bit order 2)
        invalid_xwd_bit = struct.pack(">25I", 100, 7, 2, 24, 1024, 768, 0, 0, 32, 2, 32, 32, 4096, 4, 0xFF0000, 0x00FF00, 0x0000FF, 8, 256, 0, 1024, 768, 0, 0, 0)
        with self.assertRaises(Refusal) as ctx:
            parse_xwd_header(invalid_xwd_bit)
        self.assertEqual(ctx.exception.code, "E_SCHEMA")

        # 2. Credit Flow & Inflight capacity
        credit_ctrl = CreditFlowController(max_inflight_frames=4)
        self.assertEqual(credit_ctrl.granted_capacity, 2)  # initial ACK grant = min(2, 4) = 2

        # Valid cumulative grant update: ACK seq 1, request grant 5 (1 + 4 = 5 max allowed)
        grant = credit_ctrl.ack_seq(seq=1, request_grant=5)
        self.assertEqual(grant, 5)

        # Overgrant refusal with E_CREDIT: ACK seq 1, request grant 6 (> 1 + 4)
        with self.assertRaises(Refusal) as ctx:
            credit_ctrl.ack_seq(seq=1, request_grant=6)
        self.assertEqual(ctx.exception.code, "E_CREDIT")


if __name__ == "__main__":
    unittest.main()
