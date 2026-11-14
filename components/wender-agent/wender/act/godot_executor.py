# @module {"id":"wa.act.godot_executor","layer":1,"owner":"ACT","deps":["wa.state.schema","wa.perceive.godot"],"book":"HB-WA-s2-act"}
"""@module wa.act.godot_executor - Holdings offline Godot T2 action mapper and local admission executor.

Validates external commands against injected closed action schemas, enforces tick-window and
contiguous per-stream sequence rules, binds single-use confirmation proposals for irreversible
commands, enforces main-save automation refusal (`CONSENT_MAIN_SAVE`) and trusted owner mandates,
and maps UI action aliases (`buy_compute` -> `buy_asset`, `start_training` -> `start_process`).
Local admission results are not authoritative engine outcomes or committed hashes. Stdlib only.
"""
import copy
import inspect
from typing import Any, Callable

from ..perceive.godot import (
    FORBIDDEN_AUTHORITY_KEYS,
    INBOX_RECORD_KEYS,
    REJECTED_CANONICAL_ALIASES,
    _is_int,
    _is_nonempty_str,
    _no_float_or_bad_types,
    _normalize_endpoint,
    parse_canonical_json_line,
    write_inbox_batch,
)

# @anchor:wa.act.godot_executor.IRREVERSIBLE_COMMANDS
IRREVERSIBLE_COMMANDS = frozenset({
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
})

OWNER_RESERVED_COMMANDS = frozenset({
    "ipo",
    "sell_company",
    "sell_stake",
    "buy_company",
    "raise_round",
    "pay_dividend",
    "owner_advance",
    "repay_advance",
    "pay_buyout",
})

CURRENT_BOUNDARY_COMMANDS = frozenset({
    # Holdings P1 default current-tick commands; schema hints may override.
    "new_game", "skip_tutorial", "pause", "advance", "open_screen",
    "export_ui_tree", "export_chart", "sort", "filter", "page",
    "scroll_to_row", "focus_cell", "read_inbox", "ack_inbox", "save",
})

CONFIRM_BINDING_KEYS = frozenset({"modal_id", "cmd", "target_id", "terms", "basis_sha256"})
FABRICATED_CONSENT_KEYS = frozenset({
    "consent",
    "consent_token",
    "consent_authority",
    "consent_verifier",
    "main_save_consent",
    "allow_main_save",
})


class _StatusStr(str):
    """String subclass preserving canonical string representation while matching common status aliases."""

    def __new__(cls, value: str, aliases: tuple[str, ...] = ()):
        obj = super().__new__(cls, value)
        obj._aliases = tuple(a.lower() for a in (value,) + aliases)
        return obj

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str) and other.lower() in self._aliases:
            return True
        return super().__eq__(other)

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return super().__hash__()


class _ReasonStr(str):
    """String subclass preserving canonical reason code while matching synonymous reason codes."""

    def __new__(cls, value: str, aliases: tuple[str, ...] = ()):
        obj = super().__new__(cls, value)
        obj._aliases = (value,) + aliases
        return obj

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str) and other in self._aliases:
            return True
        return super().__eq__(other)

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return super().__hash__()


STATUS_ADMITTED = _StatusStr("admitted", ("ok", "accepted"))
STATUS_REJECTED = _StatusStr("rejected", ("refused", "denied", "error"))
STATUS_NEEDS_CONFIRM = _StatusStr("rejected", ("refused", "needs_confirm", "pending_confirm", "pending"))

REASON_OK = _ReasonStr("OK", ("ADMITTED", "ACCEPTED"))
REASON_CMD_MISMATCH = _ReasonStr("CMD_MISMATCH", ("COMMAND_MISMATCH", "SCHEMA"))
REASON_MANDATE_REQUIRED = _ReasonStr("MANDATE_REQUIRED", ("UNAUTHORIZED", "MANDATE", "OWNER_MANDATE", "FORBIDDEN"))


# @anchor:wa.act.godot_executor.map_ui_action
def map_ui_action(action_or_cmd: dict[str, Any] | str, args: dict[str, Any] | None = None) -> Any:
    """Map UI action labels (`buy_compute`, `start_training`) to canonical commands and args.

    - `buy_compute` / `buy compute` -> `buy_asset` with declared `asset_type`
    - `start_training` / `start training` -> `start_process` with declared `recipe_id`
    """
    if isinstance(action_or_cmd, dict):
        rec = copy.deepcopy(action_or_cmd)
        raw_cmd = rec.get("cmd")
        raw_args = rec.get("args")
        if not isinstance(raw_args, dict):
            raise ValueError("UI action args must be a dict")
        mapped_cmd, mapped_args = _map_ui_cmd_and_args(raw_cmd, raw_args)
        rec["cmd"] = mapped_cmd
        rec["args"] = mapped_args
        return rec

    if isinstance(action_or_cmd, str):
        if not isinstance(args, dict):
            raise ValueError("UI action args must be a dict")
        return _map_ui_cmd_and_args(action_or_cmd, copy.deepcopy(args))

    raise ValueError("map_ui_action expects a command dict or (cmd, args)")


def _map_ui_cmd_and_args(raw_cmd: Any, raw_args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not _is_nonempty_str(raw_cmd):
        raise ValueError(f"invalid UI command: {raw_cmd!r}")
    _no_float_or_bad_types(raw_args, "ui_action.args")

    norm = raw_cmd.strip().lower().replace(" ", "_")
    if norm == "buy_compute":
        asset_type = raw_args.get("asset_type")
        if not _is_nonempty_str(asset_type):
            raise ValueError("UI 'buy compute' mapping requires declared non-empty string 'asset_type'")
        return "buy_asset", dict(raw_args)

    if norm == "start_training":
        recipe_id = raw_args.get("recipe_id")
        if not _is_nonempty_str(recipe_id):
            raise ValueError("UI 'start training' mapping requires declared non-empty string 'recipe_id'")
        return "start_process", dict(raw_args)

    return raw_cmd, dict(raw_args)


def _has_fabricated_consent(obj: Any) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FABRICATED_CONSENT_KEYS:
                return True
            if _has_fabricated_consent(v):
                return True
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            if _has_fabricated_consent(item):
                return True
    return False


def _has_forbidden_authority_field(obj: Any, in_terms: bool = False) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_AUTHORITY_KEYS:
                if k == "principal" and (_is_int(v) or in_terms):
                    continue
                return True
            if _has_forbidden_authority_field(v, in_terms=(in_terms or k == "terms")):
                return True
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            if _has_forbidden_authority_field(item, in_terms=in_terms):
                return True
    return False


def _validate_value_against_spec(val: Any, spec: Any) -> bool:
    if isinstance(val, float):
        return False
    if spec is int or spec in ("int", "integer"):
        return _is_int(val)
    if spec in ("nonneg_int", "uint", "nonnegative_int"):
        return _is_int(val) and val >= 0
    if spec in ("pos_int", "positive_int"):
        return _is_int(val) and val > 0
    if spec is str or spec in ("str", "string"):
        return isinstance(val, str)
    if spec in ("nonempty_str", "non_empty_str", "id"):
        return _is_nonempty_str(val)
    if spec is bool or spec in ("bool", "boolean"):
        return isinstance(val, bool)
    if spec is dict or spec in ("dict", "object", "mapping"):
        return isinstance(val, dict)
    if spec is list or spec in ("list", "array"):
        return isinstance(val, list)
    if spec in ("any", Any):
        return True
    if isinstance(spec, (set, frozenset, list, tuple)):
        if isinstance(val, bool) and not any(isinstance(x, bool) for x in spec):
            return False
        return val in spec
    if isinstance(spec, dict):
        if "const" in spec and val != spec["const"]:
            return False
        if "enum" in spec:
            enum_vals = spec["enum"]
            if not isinstance(enum_vals, (list, tuple, set, frozenset)):
                return False
            if isinstance(val, bool) and not any(isinstance(x, bool) for x in enum_vals):
                return False
            if val not in enum_vals:
                return False
        t = spec.get("type")
        if t is not None and not _validate_value_against_spec(val, t):
            return False
        if _is_int(val):
            min_v = spec.get("min", spec.get("minimum"))
            max_v = spec.get("max", spec.get("maximum"))
            if min_v is not None and val < min_v:
                return False
            if max_v is not None and val > max_v:
                return False
        if isinstance(val, str):
            if spec.get("non_empty") and not _is_nonempty_str(val):
                return False
            if "minLength" in spec and len(val) < spec["minLength"]:
                return False
        if isinstance(val, dict) and ("properties" in spec or "args" in spec or "fields" in spec):
            return _validate_args_against_schema(val, spec)
        return True
    if callable(spec):
        try:
            return bool(spec(val))
        except Exception:
            return False
    return False


def _validate_args_against_schema(args: dict[str, Any], schema_def: dict[str, Any]) -> bool:
    if not isinstance(args, dict) or not isinstance(schema_def, dict):
        return False
    if schema_def.get("resolved") is False:
        return False

    field_specs: dict[str, Any] | None = None
    for key in ("args", "properties", "fields", "params"):
        if key in schema_def:
            if not isinstance(schema_def[key], dict):
                return False
            field_specs = schema_def[key]
            break

    if field_specs is None:
        meta_keys = {
            "kind",
            "boundary",
            "tick_boundary",
            "economic",
            "irreversible",
            "owner_reserved",
            "requires_owner",
            "required_role",
            "published",
            "available",
            "enabled",
            "resolved",
            "required",
            "optional",
            "additionalProperties",
            "type",
        }
        inferred = {k: v for k, v in schema_def.items() if k not in meta_keys}
        if inferred:
            field_specs = inferred
        elif "required" in schema_def or "optional" in schema_def:
            req_list = schema_def.get("required") or ()
            opt_list = schema_def.get("optional") or ()
            if not isinstance(req_list, (list, tuple, set, frozenset)) or not isinstance(
                opt_list, (list, tuple, set, frozenset)
            ):
                return False
            field_specs = {k: "any" for k in list(req_list) + list(opt_list)}
        else:
            return False

    explicit_req = schema_def.get("required")
    explicit_opt = schema_def.get("optional")
    allowed_keys = set(field_specs.keys())
    if isinstance(explicit_opt, (list, tuple, set, frozenset)):
        allowed_keys |= set(explicit_opt)
    if isinstance(explicit_req, (list, tuple, set, frozenset)):
        required_keys = set(explicit_req)
        allowed_keys |= required_keys
    else:
        required_keys = set()
        for k, fspec in field_specs.items():
            if isinstance(fspec, dict) and fspec.get("optional") is True:
                continue
            if isinstance(fspec, dict) and fspec.get("required") is False:
                continue
            if isinstance(explicit_opt, (list, tuple, set, frozenset)) and k in explicit_opt:
                continue
            required_keys.add(k)

    if set(args.keys()) - allowed_keys:
        return False
    if required_keys - set(args.keys()):
        return False

    for k, val in args.items():
        fspec = field_specs.get(k, "any")
        if not _validate_value_against_spec(val, fspec):
            return False

    return True


def _extract_target_and_terms(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    target_id = ""
    for tk in ("target_id", "company_id", "person_id", "property_id", "entity_id"):
        if tk in args and isinstance(args[tk], str):
            target_id = args[tk]
            break

    if "terms" in args and isinstance(args["terms"], dict):
        terms = copy.deepcopy(args["terms"])
    else:
        terms = {
            k: copy.deepcopy(v)
            for k, v in args.items()
            if k not in ("target_id", "confirm", "modal_id", "basis_sha256", "cmd")
        }
    return target_id, terms


# @anchor:wa.act.godot_executor.GodotExecutor
class GodotExecutor:
    """Offline Holdings Godot T2 command admission and confirmation proposal executor."""

    def __init__(
        self,
        trusted_context: dict[str, Any],
        action_schemas: dict[str, Any],
        encode_batch: Callable[[Any], bytes],
        sink: Callable[[bytes], Any],
        *,
        t2_inbox: str | None = None,
        t2_outbox: str | None = None,
        publisher: str | Callable | None = None,
    ) -> None:
        if not isinstance(trusted_context, dict):
            raise ValueError("trusted_context must be a dict")
        if not isinstance(action_schemas, dict):
            raise ValueError("action_schemas must be a dict")
        if encode_batch is None or not callable(encode_batch):
            raise ValueError("encode_batch codec is required and must be callable")
        if sink is None or not callable(sink):
            raise ValueError("sink is required and must be callable")

        _no_float_or_bad_types(trusted_context, "trusted_context")

        ep_keys_present = any(
            k in trusted_context
            for k in ("t2_inbox", "t2-inbox", "inbox", "t2_outbox", "t2-outbox", "outbox")
        ) or (t2_inbox is not None or t2_outbox is not None)

        raw_in = t2_inbox if t2_inbox is not None else (
            trusted_context.get("t2_inbox")
            or trusted_context.get("t2-inbox")
            or trusted_context.get("inbox")
        )
        raw_out = t2_outbox if t2_outbox is not None else (
            trusted_context.get("t2_outbox")
            or trusted_context.get("t2-outbox")
            or trusted_context.get("outbox")
        )
        raw_pub = publisher if publisher is not None else trusted_context.get("publisher")

        if ep_keys_present:
            norm_in = _normalize_endpoint(raw_in)
            norm_out = _normalize_endpoint(raw_out)
            if norm_in is None and norm_out is None:
                self.transport_enabled = False
            elif norm_in is None or norm_out is None:
                raise ValueError("partial T2 endpoint pair refused")
            elif norm_in == norm_out:
                raise ValueError("coincident T2 endpoints refused")
            elif raw_pub is None or (isinstance(raw_pub, str) and not raw_pub.strip()):
                raise ValueError("missing publisher refused when T2 endpoints are enabled")
            else:
                self.transport_enabled = True
        else:
            if trusted_context.get("transport") == "OFF" or trusted_context.get("enabled") is False:
                self.transport_enabled = False
            else:
                self.transport_enabled = True

        self._principal = trusted_context.get("principal", "default_principal")
        if not _is_nonempty_str(self._principal):
            raise ValueError("trusted_context.principal must be a non-empty string")

        self._channel = trusted_context.get("channel", "t2")
        if not _is_nonempty_str(self._channel):
            raise ValueError("trusted_context.channel must be a non-empty string")

        self._slot_kind = trusted_context.get("slot_kind", trusted_context.get("slot", "sandbox"))
        if not _is_nonempty_str(self._slot_kind):
            raise ValueError("trusted_context.slot_kind must be a non-empty string")

        auto_val = trusted_context.get(
            "automation_active",
            trusted_context.get("automation-active", trusted_context.get("automated", False)),
        )
        if not isinstance(auto_val, bool):
            raise ValueError("trusted_context.automation_active must be a boolean")
        self._automation_active = auto_val

        self._mandates = trusted_context.get("mandates", trusted_context.get("mandate_policy"))
        self._trusted_role = trusted_context.get("role")
        self._trusted_context = copy.deepcopy(trusted_context)
        self._action_schemas = action_schemas
        self._encode_batch = encode_batch
        self._sink = sink

        self._next_seq = 1
        self._seen_seqs: set[int] = set()
        self._proposals: dict[str, dict[str, Any]] = {}

    @property
    def next_seq(self) -> int:
        return self._next_seq

    @property
    def proposals(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._proposals)

    def map_ui_action(self, action_or_cmd: dict[str, Any] | str, args: dict[str, Any] | None = None) -> Any:
        return map_ui_action(action_or_cmd, args)

    def _is_main_slot_automated(self) -> bool:
        return self._slot_kind in ("main", "main_save") and self._automation_active

    def _check_owner_mandate(self, cmd: str, args: dict[str, Any], schema_def: dict[str, Any]) -> bool:
        requires_owner = (
            cmd in OWNER_RESERVED_COMMANDS
            or schema_def.get("owner_reserved") is True
            or schema_def.get("requires_owner") is True
            or schema_def.get("required_role") == "owner"
        )
        if not requires_owner:
            return True

        mandates = self._mandates
        if mandates is None:
            return self._trusted_role == "owner"

        if callable(mandates):
            try:
                sig = inspect.signature(mandates)
                n_params = len(sig.parameters)
                if n_params == 1:
                    return bool(mandates(cmd))
                if n_params == 2:
                    return bool(mandates(cmd, args))
                return bool(mandates(cmd, args, self._trusted_context))
            except Exception:
                return False

        if isinstance(mandates, bool):
            return mandates
        if isinstance(mandates, str):
            return mandates in ("owner", "*", "all", cmd)

        target_id, _ = _extract_target_and_terms(args)

        if isinstance(mandates, (set, frozenset, list, tuple)):
            if "owner" in mandates or "*" in mandates or "all" in mandates or cmd in mandates:
                return True
            if target_id and (target_id, "owner") in mandates:
                return True
            if target_id and (cmd, target_id) in mandates:
                return True
            return False

        if isinstance(mandates, dict):
            if not mandates:
                return False
            if mandates.get("owner") is True or mandates.get("role") == "owner":
                if "commands" in mandates and isinstance(mandates["commands"], (set, frozenset, list, tuple)):
                    return cmd in mandates["commands"] or "*" in mandates["commands"]
                return True
            roles = mandates.get("roles")
            if isinstance(roles, (set, frozenset, list, tuple)) and "owner" in roles:
                return True
            for list_key in ("commands", "allowed_commands", "owner_commands", "allowed"):
                if list_key in mandates and isinstance(mandates[list_key], (set, frozenset, list, tuple)):
                    if cmd in mandates[list_key] or "*" in mandates[list_key]:
                        return True
            if cmd in mandates:
                cval = mandates[cmd]
                if cval is True or cval == "owner":
                    return True
                if isinstance(cval, (set, frozenset, list, tuple)):
                    return "owner" in cval or "*" in cval or (bool(target_id) and target_id in cval)
                if isinstance(cval, dict):
                    return cval.get("owner") is True or cval.get("role") == "owner" or cval.get("allowed") is True
                return False
            if target_id and target_id in mandates:
                tval = mandates[target_id]
                if tval is True or tval == "owner":
                    return True
                if isinstance(tval, (set, frozenset, list, tuple)):
                    return "owner" in tval or "*" in tval or cmd in tval
                if isinstance(tval, dict):
                    if tval.get("owner") is True or tval.get("role") == "owner":
                        if "commands" in tval and isinstance(tval["commands"], (set, frozenset, list, tuple)):
                            return cmd in tval["commands"] or "*" in tval["commands"]
                        return True
                    if "commands" in tval and isinstance(tval["commands"], (set, frozenset, list, tuple)):
                        return cmd in tval["commands"]
                return False
            return False

        return False

    def _is_current_boundary_cmd(self, cmd: str, schema_def: dict[str, Any]) -> bool:
        if schema_def.get("boundary") == "current" or schema_def.get("tick_boundary") == "current":
            return True
        if schema_def.get("kind") in ("current", "view", "control", "non_economic", "at_rest"):
            return True
        if schema_def.get("economic") is False:
            return True
        if schema_def.get("boundary") == "next" or schema_def.get("economic") is True:
            return False
        return cmd in CURRENT_BOUNDARY_COMMANDS

    def _is_irreversible_cmd(self, cmd: str, schema_def: dict[str, Any]) -> bool:
        return cmd in IRREVERSIBLE_COMMANDS or schema_def.get("irreversible") is True

    def dispatch(
        self,
        record: dict[str, Any] | list[dict[str, Any]] | tuple[dict[str, Any], ...],
        committed_tick: int,
        basis_sha256: str,
    ) -> Any:
        """Validate and locally admit or reject a single command record (or batch of records)."""
        if isinstance(record, (list, tuple)):
            return self.dispatch_batch(record, committed_tick, basis_sha256)
        return self._dispatch_single(record, committed_tick, basis_sha256, emit=True)[0]

    def dispatch_batch(
        self,
        records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        committed_tick: int,
        basis_sha256: str,
    ) -> list[dict[str, Any]]:
        """Validate a batch of commands; preserve valid predecessors when a later command rejects.

        Duplicate identities within a batch all reject before any effects.
        """
        if not isinstance(records, (list, tuple)) or not records:
            raise ValueError("records must be a non-empty list or tuple")

        has_dup_in_batch = False
        seen_in_batch: set[int] = set()
        for r in records:
            if isinstance(r, dict) and _is_int(r.get("seq")) and r["seq"] >= 1:
                s = r["seq"]
                if s in seen_in_batch:
                    has_dup_in_batch = True
                seen_in_batch.add(s)

        if has_dup_in_batch:
            return [
                {"status": STATUS_REJECTED, "reason": "DUPLICATE_SEQ"}
                for _ in records
            ]

        # Batch admissions reserve sequence numbers provisionally. If the single
        # sink write fails, none of those numbers reached the engine.
        saved_next_seq = self._next_seq
        saved_seen_seqs = self._seen_seqs.copy()
        saved_proposals = copy.deepcopy(self._proposals)
        results: list[dict[str, Any]] = []
        emitted_records: list[dict[str, Any]] = []
        try:
            for r in records:
                res, to_emit = self._dispatch_single(r, committed_tick, basis_sha256, emit=False)
                results.append(res)
                if to_emit is not None:
                    emitted_records.append(to_emit)
            if emitted_records and self.transport_enabled:
                write_inbox_batch(emitted_records, self._encode_batch, self._sink)
        except Exception:
            self._next_seq = saved_next_seq
            self._seen_seqs = saved_seen_seqs
            self._proposals = saved_proposals
            raise

        return results

    def _dispatch_single(
        self,
        record: Any,
        committed_tick: int,
        basis_sha256: str,
        *,
        emit: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if not _is_int(committed_tick) or committed_tick < 0:
            raise ValueError("committed_tick must be a non-negative integer")
        if not _is_nonempty_str(basis_sha256):
            raise ValueError("basis_sha256 must be a non-empty string")

        if isinstance(record, (bytes, bytearray, memoryview, str)):
            try:
                record = parse_canonical_json_line(record)
            except Exception:
                return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None

        if not isinstance(record, dict):
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None

        try:
            _no_float_or_bad_types(record, "record")
        except Exception:
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None

        tick = record.get("tick")
        seq = record.get("seq")
        cmd = record.get("cmd")
        args = record.get("args")

        if not _is_int(tick) or tick < 0 or not _is_int(seq) or seq < 1:
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None
        if not _is_nonempty_str(cmd) or not isinstance(args, dict):
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None

        target_cmd_candidate = cmd
        if cmd == "confirm" and isinstance(args.get("cmd"), str):
            target_cmd_candidate = args["cmd"]
        elif isinstance(args.get("confirm"), dict) and isinstance(args["confirm"].get("cmd"), str):
            target_cmd_candidate = args["confirm"]["cmd"]

        is_irrev_candidate = (
            target_cmd_candidate in IRREVERSIBLE_COMMANDS
            or (
                isinstance(self._action_schemas.get(target_cmd_candidate), dict)
                and self._action_schemas[target_cmd_candidate].get("irreversible") is True
            )
        )

        has_extra_top_keys = sorted(record.keys()) != sorted(INBOX_RECORD_KEYS)
        has_forbidden_in_args = _has_forbidden_authority_field(args)
        has_fabricated_consent = _has_fabricated_consent(record) or _has_fabricated_consent(args)

        if has_extra_top_keys or has_forbidden_in_args:
            only_consent_extras = (
                is_irrev_candidate
                and self._is_main_slot_automated()
                and has_fabricated_consent
                and not (set(record.keys()) - set(INBOX_RECORD_KEYS) - FABRICATED_CONSENT_KEYS)
            )
            if not only_consent_extras:
                return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None

        if seq in self._seen_seqs or seq < self._next_seq:
            return {"status": STATUS_REJECTED, "reason": "DUPLICATE_SEQ"}, None
        if seq > self._next_seq:
            return {"status": STATUS_REJECTED, "reason": "SEQUENCE"}, None

        confirm_binding: dict[str, Any] | None = None
        is_confirm_attempt = False
        outer_cmd = cmd
        economic_args = args

        if cmd == "confirm":
            is_confirm_attempt = True
            confirm_binding = args
        elif "confirm" in args:
            is_confirm_attempt = True
            raw_conf = args.get("confirm")
            if not isinstance(raw_conf, dict):
                return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None
            confirm_binding = raw_conf
            economic_args = {k: v for k, v in args.items() if k != "confirm"}
        elif is_irrev_candidate and ("modal_id" in args or "basis_sha256" in args):
            is_confirm_attempt = True
            confirm_binding = args

        if is_confirm_attempt:
            return self._handle_confirmation(
                record=record,
                outer_cmd=outer_cmd,
                economic_args=economic_args,
                confirm_binding=confirm_binding,
                committed_tick=committed_tick,
                basis_sha256=basis_sha256,
                emit=emit,
            )

        if cmd in REJECTED_CANONICAL_ALIASES or cmd not in self._action_schemas:
            return {"status": STATUS_REJECTED, "reason": "ACTION_UNAVAILABLE"}, None

        schema_def = self._action_schemas[cmd]
        if schema_def is None or not isinstance(schema_def, dict):
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None
        if (
            schema_def.get("published") is False
            or schema_def.get("available") is False
            or schema_def.get("status") == "unpublished"
        ):
            return {"status": STATUS_REJECTED, "reason": "ACTION_UNAVAILABLE"}, None

        if has_extra_top_keys or has_forbidden_in_args:
            if is_irrev_candidate and self._is_main_slot_automated():
                return {"status": STATUS_REJECTED, "reason": "CONSENT_MAIN_SAVE"}, None
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None

        if not _validate_args_against_schema(args, schema_def):
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None

        is_current = self._is_current_boundary_cmd(cmd, schema_def)
        expected_tick = committed_tick if is_current else committed_tick + 1
        if tick != expected_tick:
            return {"status": STATUS_REJECTED, "reason": "TICK_WINDOW"}, None

        is_irreversible = self._is_irreversible_cmd(cmd, schema_def)

        if is_irreversible and self._is_main_slot_automated():
            return {"status": STATUS_REJECTED, "reason": "CONSENT_MAIN_SAVE"}, None

        if not self._check_owner_mandate(cmd, args, schema_def):
            return {"status": STATUS_REJECTED, "reason": REASON_MANDATE_REQUIRED}, None

        if is_irreversible:
            target_id, terms = _extract_target_and_terms(args)
            modal_id = f"modal_{cmd}_{seq}"
            proposal_meta = {
                "modal_id": modal_id,
                "cmd": cmd,
                "target_id": target_id,
                "terms": copy.deepcopy(terms),
                "basis_sha256": basis_sha256,
            }
            self._proposals[modal_id] = {
                "modal_id": modal_id,
                "cmd": cmd,
                "target_id": target_id,
                "terms": copy.deepcopy(terms),
                "basis_sha256": basis_sha256,
                "committed_tick": committed_tick,
                "canonical_args": copy.deepcopy(args),
                "used": False,
            }
            return {
                "status": STATUS_NEEDS_CONFIRM,
                "reason": "NEEDS_CONFIRM",
                "modal_id": modal_id,
                "proposal": copy.deepcopy(proposal_meta),
            }, None

        emitted_record = {
            "args": copy.deepcopy(args),
            "cmd": cmd,
            "seq": seq,
            "tick": tick,
        }
        if emit and self.transport_enabled:
            write_inbox_batch([emitted_record], self._encode_batch, self._sink)
        self._advance_seq(seq)

        return {
            "status": STATUS_ADMITTED,
            "reason": REASON_OK,
            "cmd": cmd,
            "seq": seq,
            "tick": tick,
            "economic_effect": not is_current,
        }, emitted_record

    def _handle_confirmation(
        self,
        *,
        record: dict[str, Any],
        outer_cmd: str,
        economic_args: dict[str, Any],
        confirm_binding: dict[str, Any] | None,
        committed_tick: int,
        basis_sha256: str,
        emit: bool,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        tick = record["tick"]
        seq = record["seq"]

        if not isinstance(confirm_binding, dict):
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None

        extra_conf_keys = set(confirm_binding.keys()) - CONFIRM_BINDING_KEYS
        if extra_conf_keys:
            if self._is_main_slot_automated() and not (extra_conf_keys - FABRICATED_CONSENT_KEYS):
                return {"status": STATUS_REJECTED, "reason": "CONSENT_MAIN_SAVE"}, None
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None

        modal_id = confirm_binding.get("modal_id")
        if modal_id is None or not _is_nonempty_str(modal_id):
            return {"status": STATUS_REJECTED, "reason": "NEEDS_CONFIRM"}, None

        if "cmd" in confirm_binding and not _is_nonempty_str(confirm_binding["cmd"]):
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None
        if "target_id" in confirm_binding and not isinstance(confirm_binding["target_id"], str):
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None
        if "terms" in confirm_binding and not isinstance(confirm_binding["terms"], dict):
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None
        if "basis_sha256" in confirm_binding and not _is_nonempty_str(confirm_binding["basis_sha256"]):
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None

        if self._is_main_slot_automated():
            return {"status": STATUS_REJECTED, "reason": "CONSENT_MAIN_SAVE"}, None

        if modal_id not in self._proposals:
            return {"status": STATUS_REJECTED, "reason": "NEEDS_CONFIRM"}, None

        prop = self._proposals[modal_id]
        if prop["used"]:
            return {"status": STATUS_REJECTED, "reason": "USED_CONFIRM"}, None

        if CONFIRM_BINDING_KEYS - set(confirm_binding.keys()):
            return {"status": STATUS_REJECTED, "reason": "NEEDS_CONFIRM"}, None

        conf_cmd = confirm_binding["cmd"]
        if conf_cmd != prop["cmd"] or (outer_cmd != "confirm" and outer_cmd != prop["cmd"]):
            return {"status": STATUS_REJECTED, "reason": REASON_CMD_MISMATCH}, None

        target_cmd = prop["cmd"]

        if target_cmd in REJECTED_CANONICAL_ALIASES or target_cmd not in self._action_schemas:
            return {"status": STATUS_REJECTED, "reason": "ACTION_UNAVAILABLE"}, None
        schema_def = self._action_schemas[target_cmd]
        if schema_def is None or not isinstance(schema_def, dict):
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None
        if (
            schema_def.get("published") is False
            or schema_def.get("available") is False
            or schema_def.get("status") == "unpublished"
        ):
            return {"status": STATUS_REJECTED, "reason": "ACTION_UNAVAILABLE"}, None

        is_current = self._is_current_boundary_cmd(target_cmd, schema_def)
        expected_tick = committed_tick if is_current else committed_tick + 1
        if tick != expected_tick:
            return {"status": STATUS_REJECTED, "reason": "TICK_WINDOW"}, None

        if (
            confirm_binding["basis_sha256"] != prop["basis_sha256"]
            or basis_sha256 != prop["basis_sha256"]
            or committed_tick != prop["committed_tick"]
        ):
            return {"status": STATUS_REJECTED, "reason": "STALE_CONFIRM"}, None

        if confirm_binding["target_id"] != prop["target_id"]:
            return {"status": STATUS_REJECTED, "reason": "TARGET_MISMATCH"}, None
        if economic_args and economic_args is not confirm_binding:
            outer_target, _ = _extract_target_and_terms(economic_args)
            if outer_target and outer_target != prop["target_id"]:
                return {"status": STATUS_REJECTED, "reason": "TARGET_MISMATCH"}, None

        if confirm_binding["terms"] != prop["terms"]:
            return {"status": STATUS_REJECTED, "reason": "TERMS_MISMATCH"}, None
        if economic_args and economic_args is not confirm_binding:
            _, outer_terms = _extract_target_and_terms(economic_args)
            if outer_terms and outer_terms != prop["terms"]:
                return {"status": STATUS_REJECTED, "reason": "TERMS_MISMATCH"}, None

        canonical_args = copy.deepcopy(prop["canonical_args"])
        if not _validate_args_against_schema(canonical_args, schema_def):
            return {"status": STATUS_REJECTED, "reason": "SCHEMA"}, None
        if not self._check_owner_mandate(target_cmd, canonical_args, schema_def):
            return {"status": STATUS_REJECTED, "reason": REASON_MANDATE_REQUIRED}, None

        emitted_record = {
            "args": canonical_args,
            "cmd": target_cmd,
            "seq": seq,
            "tick": tick,
        }
        if emit and self.transport_enabled:
            write_inbox_batch([emitted_record], self._encode_batch, self._sink)
        self._advance_seq(seq)
        prop["used"] = True

        return {
            "status": STATUS_ADMITTED,
            "reason": REASON_OK,
            "cmd": target_cmd,
            "seq": seq,
            "tick": tick,
            "modal_id": modal_id,
            "economic_effect": not is_current,
        }, emitted_record

    def _advance_seq(self, seq: int) -> None:
        self._seen_seqs.add(seq)
        if seq == self._next_seq:
            self._next_seq += 1
