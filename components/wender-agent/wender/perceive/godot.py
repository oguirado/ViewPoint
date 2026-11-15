# @module {"id":"wa.perceive.godot","layer":1,"owner":"PERCEIVE","deps":["wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.godot - Holdings offline Godot T2 perception client, UI-tree validator and mailbox framing.

Validates base `wender.state/v1` (with `source.kind == "godot"`) paired with the closed
`holdings.godot.uitree/v1` sidecar, parses B3 UI state tokens against injected registries,
and reads/writes canonical UTF-8 LF-delimited integer JSON record batches through injected
session-owned T2 mailbox codecs. Stdlib only.
"""
from collections import namedtuple
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Callable

from ..state import schema

UITREE_SCHEMA_ID = "holdings.godot.uitree/v1"
SIDECAR_TOP_KEYS = ("base_sha256", "nodes", "schema")
SIDECAR_BASE_NODE_KEYS = ("control_path", "screen_id")
SIDECAR_LINK_KEYS = ("col_key", "row_id", "table_id")
SIDECAR_TABLE_SUMMARY_KEYS = ("as_of_tick", "query", "refresh_cadence", "visible_range")
SIDECAR_CHART_SUMMARY_KEYS = ("as_of_tick", "refresh_cadence")
SIDECAR_ALL_NODE_KEYS = frozenset(
    SIDECAR_BASE_NODE_KEYS + SIDECAR_LINK_KEYS + SIDECAR_TABLE_SUMMARY_KEYS
)
VISIBLE_RANGE_KEYS = ("count", "first_row_id", "last_row_id")
QUERY_KEYS = ("filter_spec", "page_index", "page_size", "sort_dir", "sort_key")
FILTER_SPEC_KEYS = ("col_key", "op", "value")
SORT_DIRS = ("asc", "desc")

B3_UNPARAMETERIZED_FAMILIES = frozenset({"simulating", "paused", "awaiting_input"})
B3_PARAMETERIZED_FAMILIES = frozenset({"modal", "busy", "error"})
B3_FAMILIES = B3_UNPARAMETERIZED_FAMILIES | B3_PARAMETERIZED_FAMILIES

TABLE_ROLES = frozenset({"table", "grid", "treegrid"})
CHART_ROLES = frozenset({"chart", "figure", "plot", "graph", "graphics-document"})
ROW_ROLES = frozenset({"row"})
CELL_ROLES = frozenset({"cell", "gridcell", "columnheader", "rowheader"})

INBOX_RECORD_KEYS = ("args", "cmd", "seq", "tick")
FORBIDDEN_AUTHORITY_KEYS = frozenset({
    "actor",
    "channel",
    "principal",
    "role",
    "slot",
    "slot_kind",
    "consent",
    "consent_token",
    "consent_authority",
    "consent_verifier",
    "main_save_consent",
    "allow_main_save",
    "automation_active",
    "automation-active",
    "mandates",
    "order_seq_id",
})
REJECTED_CANONICAL_ALIASES = frozenset({"buy_compute", "start_training"})

_SCOPED_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_SLASH_ROOT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
OUTCOME_RECORD_KEYS = frozenset({
    "seq", "requested_tick", "applied_tick", "order_seq_id",
    "status", "reason", "state_sha256",
})
OUTCOME_STATUSES = frozenset({"accepted", "rejected", "proposed"})
_HEX16_RE = re.compile(r"^[0-9a-f]{16}\Z")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_UNICODE_LINE_SEPS = ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")

ParsedUiState = namedtuple("ParsedUiState", ("family", "suffix"))
OutboxReadResult = namedtuple("OutboxReadResult", ("batches", "pending"))


# @anchor:wa.perceive.godot.GodotError
class GodotError(ValueError):
    """Raised when Godot T2 perception, mailbox framing or configuration fails validation."""


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_nonempty_str(x: Any) -> bool:
    return isinstance(x, str) and bool(x) and x == x.strip() and not any(c in x for c in _UNICODE_LINE_SEPS)


def _ensure_valid_utf8_str(s: str, path: str = "$") -> None:
    if not isinstance(s, str):
        raise GodotError(f"expected string at {path}")
    try:
        s.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise GodotError(f"invalid UTF-8 string at {path}: {exc}") from exc


def _no_float_or_bad_types(obj: Any, path: str = "$") -> None:
    if isinstance(obj, float):
        raise GodotError(f"float not allowed at {path}")
    if isinstance(obj, str):
        _ensure_valid_utf8_str(obj, path)
        return
    if obj is None or isinstance(obj, bool) or _is_int(obj):
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, float):
                raise GodotError(f"float key not allowed at {path}")
            if not isinstance(k, str):
                raise GodotError(f"non-string key at {path}: {type(k)!r}")
            _ensure_valid_utf8_str(k, f"{path}.<key>")
            _no_float_or_bad_types(v, f"{path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            _no_float_or_bad_types(item, f"{path}[{idx}]")
        return
    raise GodotError(f"unsupported value type at {path}: {type(obj)!r}")


def _strict_object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if not isinstance(k, str):
            raise GodotError("JSON object key must be a string")
        _ensure_valid_utf8_str(k, "$.key")
        if k in out:
            raise GodotError(f"duplicate JSON key: {k!r}")
        out[k] = v
    return out


def _reject_float_token(token: str) -> None:
    raise GodotError(f"float token not allowed in integer JSON: {token!r}")


def _reject_constant_token(token: str) -> None:
    raise GodotError(f"JSON constant not allowed: {token!r}")


def parse_canonical_json_line(raw_line: bytes | str) -> dict[str, Any]:
    """Parse a single UTF-8 JSON-L record rejecting duplicate keys, floats and non-LF separators."""
    if isinstance(raw_line, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(raw_line)
        if b"\n" in raw_bytes or b"\r" in raw_bytes:
            raise GodotError("embedded CR/LF not allowed inside a single JSON record line")
        try:
            text = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise GodotError(f"invalid UTF-8 encoding in JSON line: {exc}") from exc
    elif isinstance(raw_line, str):
        if "\n" in raw_line or "\r" in raw_line:
            raise GodotError("embedded CR/LF not allowed inside a single JSON record line")
        _ensure_valid_utf8_str(raw_line, "$")
        text = raw_line
    else:
        raise GodotError("JSON line must be bytes or str")

    if not text or text != text.strip(" \t\r"):
        raise GodotError("empty or whitespace-padded JSON line")

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_strict_object_pairs_hook,
            parse_float=_reject_float_token,
            parse_constant=_reject_constant_token,
        )
    except GodotError:
        raise
    except Exception as exc:
        raise GodotError(f"malformed JSON record line: {exc}") from exc

    if not isinstance(parsed, dict):
        raise GodotError("JSON record line must decode to an object")
    _no_float_or_bad_types(parsed)
    return parsed


# @anchor:wa.perceive.godot.is_valid_control_path
def is_valid_control_path(path: Any) -> bool:
    """Accept legacy dot paths and Holdings screen-rooted slash paths.

    Dot paths retain their strict identifier grammar. Slash paths begin with a
    screen id and contain non-empty segments other than "." or "..";
    whitespace, Unicode controls and invalid UTF-8 are forbidden throughout.
    """
    if not isinstance(path, str):
        return False
    if _SCOPED_PATH_RE.fullmatch(path):
        return True
    if "/" not in path:
        return False
    try:
        path.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    if any(char.isspace() or unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in path):
        return False
    parts = path.split("/")
    return (len(parts) >= 2 and bool(_SLASH_ROOT_RE.fullmatch(parts[0]))
            and all(part not in ("", ".", "..") for part in parts[1:]))


# @anchor:wa.perceive.godot.node_id_for_control_path
def node_id_for_control_path(path: str) -> str:
    """Return the first sixteen lowercase SHA-256 hex characters of UTF-8 `path`."""
    if not isinstance(path, str):
        raise ValueError("control_path must be a string")
    try:
        raw = path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"control_path must be valid UTF-8: {exc}") from exc
    return hashlib.sha256(raw).hexdigest()[:16]


def _lookup_registry_set(registries: dict[str, Any], family: str) -> set[str]:
    key_candidates = {
        "modal": ("modal", "modals", "modal_ids"),
        "busy": ("busy", "busy_operations", "busy_ops", "operations"),
        "error": ("error", "errors", "error_literals", "error_codes"),
    }[family]
    found = None
    for k in key_candidates:
        if k in registries:
            found = registries[k]
            break
    if found is None or isinstance(found, (str, bytes)) or not isinstance(found, (set, frozenset, list, tuple, dict)):
        raise ValueError(f"missing or invalid registry for UI state family {family!r}")
    items = found.keys() if isinstance(found, dict) else found
    out: set[str] = set()
    for item in items:
        if not isinstance(item, str) or not item or item != item.strip():
            raise ValueError(f"invalid registered token in {family!r} registry: {item!r}")
        out.add(item)
    return out


# @anchor:wa.perceive.godot.parse_ui_state
def parse_ui_state(token: str, registries: dict[str, Any] | None = None) -> ParsedUiState:
    """Parse a B3 UI state token into `(family, suffix)` or raise `ValueError`."""
    if not isinstance(token, str) or not token or token != token.strip() or any(c.isspace() for c in token):
        raise ValueError(f"invalid UI state token: {token!r}")
    if registries is not None and not isinstance(registries, dict):
        raise ValueError("registries must be a mapping")

    family, sep, suffix = token.partition(":")
    if family in B3_UNPARAMETERIZED_FAMILIES:
        if sep or suffix:
            raise ValueError(f"UI state family {family!r} forbids a suffix: {token!r}")
        return ParsedUiState(family, None)

    if family in B3_PARAMETERIZED_FAMILIES:
        if not sep or not suffix or ":" in suffix:
            raise ValueError(f"UI state family {family!r} requires a non-empty suffix: {token!r}")
        if not isinstance(registries, dict):
            raise ValueError(f"UI state family {family!r} requires injected registries")
        allowed = _lookup_registry_set(registries, family)
        if suffix not in allowed:
            raise ValueError(f"unregistered {family!r} value: {suffix!r}")
        return ParsedUiState(family, suffix)

    raise ValueError(f"unknown UI state family: {family!r}")


def _extract_validation_registries(
    registries_or_cadences: Any = None,
    screens: Any = None,
    *,
    cadences: Any = None,
    cadence_registry: Any = None,
    refresh_cadences: Any = None,
    scene_inventory: Any = None,
    registries: Any = None,
) -> tuple[set[str] | None, set[str] | None, dict[str, Any] | None, list[str]]:
    errs: list[str] = []
    cadence_src = cadences if cadences is not None else (
        cadence_registry if cadence_registry is not None else refresh_cadences
    )
    screen_src = screens if screens is not None else scene_inventory
    ui_reg = registries if isinstance(registries, dict) else None

    if registries_or_cadences is not None:
        if isinstance(registries_or_cadences, dict):
            for k in ("cadences", "cadence_registry", "refresh_cadences"):
                if k in registries_or_cadences and cadence_src is None:
                    cadence_src = registries_or_cadences[k]
            for k in ("screens", "scene_inventory", "screen_ids"):
                if k in registries_or_cadences and screen_src is None:
                    screen_src = registries_or_cadences[k]
            if ui_reg is None:
                ui_reg = registries_or_cadences
        else:
            if cadence_src is None:
                cadence_src = registries_or_cadences

    def _to_str_set(src: Any, label: str) -> set[str] | None:
        if src is None:
            return None
        if isinstance(src, (str, bytes)) or not isinstance(src, (set, frozenset, list, tuple, dict)):
            errs.append(f"{label} must be a collection of strings")
            return set()
        vals = src.keys() if isinstance(src, dict) else src
        out: set[str] = set()
        for v in vals:
            if not _is_nonempty_str(v):
                errs.append(f"{label} contains invalid entry {v!r}")
            else:
                out.add(v)
        return out

    return _to_str_set(cadence_src, "cadence_registry"), _to_str_set(screen_src, "scene_inventory"), ui_reg, errs


def _validate_visible_range(vr: Any, prefix: str, violations: list[str]) -> None:
    if not isinstance(vr, dict):
        violations.append(f"{prefix}.visible_range must be an object")
        return
    if sorted(vr.keys()) != sorted(VISIBLE_RANGE_KEYS):
        violations.append(f"{prefix}.visible_range keys {sorted(vr.keys())} != {list(VISIBLE_RANGE_KEYS)}")
        return
    cnt = vr["count"]
    first_id = vr["first_row_id"]
    last_id = vr["last_row_id"]
    if not _is_int(cnt) or cnt < 0:
        violations.append(f"{prefix}.visible_range.count must be a non-negative integer")
        return
    if first_id is not None and not _is_nonempty_str(first_id):
        violations.append(f"{prefix}.visible_range.first_row_id must be a non-empty string or null")
    if last_id is not None and not _is_nonempty_str(last_id):
        violations.append(f"{prefix}.visible_range.last_row_id must be a non-empty string or null")
    if cnt == 0:
        if first_id is not None or last_id is not None:
            violations.append(f"{prefix}.visible_range empty range requires null endpoints")
    else:
        if first_id is None or last_id is None:
            violations.append(f"{prefix}.visible_range non-empty range requires string endpoints")
        elif cnt == 1 and first_id != last_id:
            violations.append(f"{prefix}.visible_range count == 1 requires first_row_id == last_row_id")
        elif cnt > 1 and first_id == last_id:
            violations.append(f"{prefix}.visible_range count > 1 requires distinct first_row_id and last_row_id")


def _validate_query(q: Any, prefix: str, violations: list[str]) -> None:
    if not isinstance(q, dict):
        violations.append(f"{prefix}.query must be an object")
        return
    if sorted(q.keys()) != sorted(QUERY_KEYS):
        violations.append(f"{prefix}.query keys {sorted(q.keys())} != {list(QUERY_KEYS)}")
        return
    if not _is_nonempty_str(q["sort_key"]):
        violations.append(f"{prefix}.query.sort_key must be a non-empty string")
    if q["sort_dir"] not in SORT_DIRS:
        violations.append(f"{prefix}.query.sort_dir must be 'asc' or 'desc'")
    if not _is_int(q["page_index"]) or q["page_index"] < 0:
        violations.append(f"{prefix}.query.page_index must be a non-negative integer")
    if not _is_int(q["page_size"]) or q["page_size"] <= 0:
        violations.append(f"{prefix}.query.page_size must be a positive integer")
    fspec = q["filter_spec"]
    if not isinstance(fspec, list):
        violations.append(f"{prefix}.query.filter_spec must be an array")
        return
    for idx, item in enumerate(fspec):
        ip = f"{prefix}.query.filter_spec[{idx}]"
        if not isinstance(item, dict):
            violations.append(f"{ip} must be an object")
            continue
        if sorted(item.keys()) != sorted(FILTER_SPEC_KEYS):
            violations.append(f"{ip} keys {sorted(item.keys())} != {list(FILTER_SPEC_KEYS)}")
            continue
        if not _is_nonempty_str(item["col_key"]):
            violations.append(f"{ip}.col_key must be a non-empty string")
        if item["op"] != "eq":
            violations.append(f"{ip}.op must be 'eq'")
        if not isinstance(item["value"], str):
            violations.append(f"{ip}.value must be a string")


def _validate_cadence_and_tick(
    entry: dict[str, Any],
    prefix: str,
    cadences: set[str] | None,
    violations: list[str],
) -> None:
    if "as_of_tick" not in entry:
        violations.append(f"{prefix} missing required summary field 'as_of_tick'")
    elif not _is_int(entry["as_of_tick"]) or entry["as_of_tick"] < 0:
        violations.append(f"{prefix}.as_of_tick must be a non-negative integer")

    if "refresh_cadence" not in entry:
        violations.append(f"{prefix} missing required summary field 'refresh_cadence'")
    elif not _is_nonempty_str(entry["refresh_cadence"]):
        violations.append(f"{prefix}.refresh_cadence must be a non-empty string")
    elif cadences is not None and entry["refresh_cadence"] not in cadences:
        violations.append(f"{prefix}.refresh_cadence {entry['refresh_cadence']!r} not in cadence registry")


# @anchor:wa.perceive.godot.validate_uitree
def validate_uitree(
    base: Any,
    sidecar: Any,
    registries_or_cadences: Any = None,
    screens: Any = None,
    *,
    cadences: Any = None,
    cadence_registry: Any = None,
    refresh_cadences: Any = None,
    scene_inventory: Any = None,
    registries: Any = None,
) -> list[str]:
    """Validate a Godot `wender.state/v1` base record and its `holdings.godot.uitree/v1` sidecar.

    Returns a list of violation descriptions (empty list iff valid).
    """
    violations: list[str] = []

    cadence_set, screen_set, ui_reg, reg_errs = _extract_validation_registries(
        registries_or_cadences,
        screens,
        cadences=cadences,
        cadence_registry=cadence_registry,
        refresh_cadences=refresh_cadences,
        scene_inventory=scene_inventory,
        registries=registries,
    )
    violations.extend(reg_errs)

    if not isinstance(base, dict):
        return ["base must be an object"]
    if not isinstance(sidecar, dict):
        return ["sidecar must be an object"]

    base_errs = schema.validate_state(base)
    violations.extend(base_errs)

    try:
        _no_float_or_bad_types(base, "base")
    except GodotError as exc:
        violations.append(str(exc))

    try:
        _no_float_or_bad_types(sidecar, "sidecar")
    except GodotError as exc:
        violations.append(str(exc))

    src = base.get("source")
    if not isinstance(src, dict) or src.get("kind") != "godot":
        violations.append("base source.kind must be 'godot'")
    elif sorted(src.keys()) != ["kind", "title", "url"]:
        violations.append(f"base source keys {sorted(src.keys())} != ['kind', 'title', 'url']")
    else:
        if src.get("url") is not None and not isinstance(src.get("url"), str):
            violations.append("base source.url must be string or null")
        if src.get("title") is not None and not isinstance(src.get("title"), str):
            violations.append("base source.title must be string or null")

    prune = base.get("prune")
    if not isinstance(prune, dict) or sorted(prune.keys()) != ["name", "version"]:
        violations.append("base prune must have exact keys ['name', 'version']")
    else:
        if not isinstance(prune.get("name"), str) or not prune.get("name"):
            violations.append("base prune.name must be a non-empty string")
        if not _is_int(prune.get("version")) or prune.get("version") < 1:
            violations.append("base prune.version must be a positive integer")

    counts = base.get("counts")
    if not isinstance(counts, dict) or sorted(counts.keys()) != ["actionable", "nodes", "raw"]:
        violations.append("base counts must have exact keys ['actionable', 'nodes', 'raw']")
    elif _is_int(counts.get("raw")) and counts["raw"] < len(base.get("nodes") or []):
        violations.append("base counts.raw must be >= counts.nodes")

    base_nodes_by_id: dict[str, dict[str, Any]] = {}
    base_node_order: list[str] = []
    raw_base_nodes = base.get("nodes")
    if isinstance(raw_base_nodes, list):
        for idx, n in enumerate(raw_base_nodes):
            if not isinstance(n, dict):
                continue
            nid = n.get("id")
            parent_id = n.get("parent")
            if isinstance(nid, str):
                if parent_id == nid:
                    violations.append(f"base.nodes[{idx}] cannot be its own parent")
                elif parent_id is not None and parent_id in base_nodes_by_id:
                    pnode = base_nodes_by_id[parent_id]
                    if _is_int(n.get("depth")) and _is_int(pnode.get("depth")) and n["depth"] <= pnode["depth"]:
                        violations.append(f"base.nodes[{idx}].depth must exceed parent depth")
                elif parent_id is None and _is_int(n.get("depth")) and n["depth"] != 0:
                    violations.append(f"base.nodes[{idx}] root node must have depth 0")
                if nid not in base_nodes_by_id:
                    base_nodes_by_id[nid] = n
                    base_node_order.append(nid)
            states = n.get("states")
            if isinstance(states, dict) and "ui_state" in states and ui_reg is not None:
                try:
                    parse_ui_state(states["ui_state"], ui_reg)
                except ValueError as exc:
                    violations.append(f"base.nodes[{idx}].states.ui_state invalid: {exc}")

    expected_base_sha256: str | None = None
    try:
        canonical_base_json = schema.dumps(base)
        expected_base_sha256 = hashlib.sha256(canonical_base_json.encode("utf-8")).hexdigest()
    except Exception as exc:
        violations.append(f"base canonical serialization failed: {exc}")

    if sorted(sidecar.keys()) != sorted(SIDECAR_TOP_KEYS):
        violations.append(f"sidecar top-level keys {sorted(sidecar.keys())} != {list(SIDECAR_TOP_KEYS)}")
        return violations

    if sidecar["schema"] != UITREE_SCHEMA_ID:
        violations.append(f"sidecar.schema must be {UITREE_SCHEMA_ID!r}")

    bsha = sidecar["base_sha256"]
    if not isinstance(bsha, str) or not _HEX64_RE.match(bsha):
        violations.append("sidecar.base_sha256 must be 64 lowercase hex characters")
    elif expected_base_sha256 is not None and bsha != expected_base_sha256:
        violations.append(f"sidecar.base_sha256 digest mismatch: got {bsha}, expected {expected_base_sha256}")

    sc_nodes = sidecar["nodes"]
    if not isinstance(sc_nodes, dict):
        violations.append("sidecar.nodes must be an object mapping node id to sidecar entry")
        return violations

    base_id_set = set(base_nodes_by_id.keys())
    sidecar_id_set = set(sc_nodes.keys())
    missing_ids = sorted(base_id_set - sidecar_id_set)
    orphan_ids = sorted(sidecar_id_set - base_id_set)
    if missing_ids:
        violations.append(f"sidecar.nodes missing base node ids: {missing_ids}")
    if orphan_ids:
        violations.append(f"sidecar.nodes contains orphan node ids: {orphan_ids}")

    seen_paths: dict[str, str] = {}
    seen_hashes: dict[str, str] = {}
    table_summaries: dict[str, tuple[str, dict[str, Any]]] = {}
    table_rows_by_table: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    seen_row_keys: set[tuple[str, str]] = set()
    seen_cell_keys: set[tuple[str, str, str]] = set()

    ordered_nids = [nid for nid in base_node_order if nid in sc_nodes] + [
        nid for nid in sc_nodes if nid not in base_nodes_by_id
    ]

    for nid in ordered_nids:
        entry = sc_nodes[nid]
        prefix = f"sidecar.nodes[{nid!r}]"
        if not isinstance(nid, str) or not _HEX16_RE.match(nid):
            violations.append(f"{prefix} key must be 16 lowercase hex characters")
        if not isinstance(entry, dict):
            violations.append(f"{prefix} must be an object")
            continue

        unknown_keys = sorted(set(entry.keys()) - SIDECAR_ALL_NODE_KEYS)
        if unknown_keys:
            violations.append(f"{prefix} contains unknown fields: {unknown_keys}")

        cpath = entry.get("control_path")
        if not isinstance(cpath, str) or not is_valid_control_path(cpath):
            violations.append(f"{prefix}.control_path invalid stable scoped path: {cpath!r}")
        else:
            computed_id = node_id_for_control_path(cpath)
            if computed_id != nid:
                violations.append(
                    f"{prefix}.control_path hash mismatch: path {cpath!r} hashes to {computed_id}, key is {nid}"
                )
            if cpath in seen_paths and seen_paths[cpath] != nid:
                violations.append(f"{prefix}.control_path duplicate path {cpath!r}")
            seen_paths[cpath] = nid
            if computed_id in seen_hashes and seen_hashes[computed_id] != cpath:
                violations.append(f"{prefix}.control_path hash collision on {computed_id}")
            seen_hashes[computed_id] = cpath

        screen_id = entry.get("screen_id")
        if not _is_nonempty_str(screen_id):
            violations.append(f"{prefix}.screen_id must be a non-empty string")
        elif screen_set is not None and screen_id not in screen_set:
            violations.append(f"{prefix}.screen_id {screen_id!r} not in scene inventory")
        if isinstance(cpath, str) and "/" in cpath and isinstance(screen_id, str):
            if cpath.split("/", 1)[0] != screen_id:
                violations.append(f"{prefix}.control_path must be rooted at screen_id {screen_id!r}")

        for lk in SIDECAR_LINK_KEYS:
            if lk in entry and not _is_nonempty_str(entry[lk]):
                violations.append(f"{prefix}.{lk} must be a non-empty string when present")

        has_table_id = "table_id" in entry
        has_row_id = "row_id" in entry
        has_col_key = "col_key" in entry

        base_node = base_nodes_by_id.get(nid)
        role = base_node.get("role") if isinstance(base_node, dict) else None

        is_table_role = role in TABLE_ROLES
        is_chart_role = role in CHART_ROLES
        is_row_role = role in ROW_ROLES
        is_cell_role = role in CELL_ROLES
        is_table_summary_linkage = has_table_id and not has_row_id and not has_col_key

        if is_table_role or is_table_summary_linkage:
            if not is_table_role:
                violations.append(f"{prefix} has table summary linkage but base role is {role!r}")
            if has_row_id or has_col_key:
                violations.append(f"{prefix} table summary node forbids row_id and col_key")
            for req_k in SIDECAR_TABLE_SUMMARY_KEYS:
                if req_k not in entry:
                    violations.append(f"{prefix} table summary missing required field {req_k!r}")
            if "visible_range" in entry:
                _validate_visible_range(entry["visible_range"], prefix, violations)
            if "query" in entry:
                _validate_query(entry["query"], prefix, violations)
            _validate_cadence_and_tick(entry, prefix, cadence_set, violations)

            if has_table_id and _is_nonempty_str(entry["table_id"]):
                tid = entry["table_id"]
                if tid in table_summaries:
                    violations.append(f"{prefix}.table_id duplicate table summary id {tid!r}")
                else:
                    table_summaries[tid] = (nid, entry)
        elif is_chart_role:
            if has_table_id or has_row_id or has_col_key:
                violations.append(f"{prefix} chart summary forbids table/row/cell linkage fields")
            for forbidden_k in ("visible_range", "query"):
                if forbidden_k in entry:
                    violations.append(f"{prefix} chart summary forbids table field {forbidden_k!r}")
            _validate_cadence_and_tick(entry, prefix, cadence_set, violations)
        else:
            for forbidden_k in SIDECAR_TABLE_SUMMARY_KEYS:
                if forbidden_k in entry:
                    violations.append(f"{prefix} non-summary node (role={role!r}) forbids {forbidden_k!r}")

            if is_row_role or (has_row_id and not has_col_key):
                if not is_row_role:
                    violations.append(f"{prefix} has row linkage but base role is {role!r}")
                if not has_table_id or not has_row_id or has_col_key:
                    violations.append(f"{prefix} row node requires table_id and row_id and forbids col_key")
                elif _is_nonempty_str(entry["table_id"]) and _is_nonempty_str(entry["row_id"]):
                    rk = (entry["table_id"], entry["row_id"])
                    if rk in seen_row_keys:
                        violations.append(f"{prefix} duplicate row linkage {rk!r}")
                    seen_row_keys.add(rk)
                    table_rows_by_table.setdefault(entry["table_id"], []).append((nid, entry["row_id"], entry))
            elif is_cell_role or has_col_key:
                if not is_cell_role:
                    violations.append(f"{prefix} has cell linkage but base role is {role!r}")
                if not (has_table_id and has_row_id and has_col_key):
                    violations.append(f"{prefix} cell node requires table_id, row_id and col_key")
                elif (
                    _is_nonempty_str(entry["table_id"])
                    and _is_nonempty_str(entry["row_id"])
                    and _is_nonempty_str(entry["col_key"])
                ):
                    ck = (entry["table_id"], entry["row_id"], entry["col_key"])
                    if ck in seen_cell_keys:
                        violations.append(f"{prefix} duplicate cell linkage {ck!r}")
                    seen_cell_keys.add(ck)
            else:
                if has_table_id or has_row_id or has_col_key:
                    violations.append(
                        f"{prefix} node with role {role!r} has inconsistent table/row/cell linkage"
                    )

    for nid in ordered_nids:
        entry = sc_nodes.get(nid)
        base_node = base_nodes_by_id.get(nid)
        if not isinstance(entry, dict) or not isinstance(base_node, dict):
            continue
        role = base_node.get("role")
        parent_id = base_node.get("parent")
        parent_entry = sc_nodes.get(parent_id) if isinstance(parent_id, str) else None
        parent_base = base_nodes_by_id.get(parent_id) if isinstance(parent_id, str) else None
        prefix = f"sidecar.nodes[{nid!r}]"

        if role in ROW_ROLES:
            tid = entry.get("table_id")
            if _is_nonempty_str(tid) and tid not in table_summaries:
                violations.append(f"{prefix} row references unknown table_id {tid!r}")
            if isinstance(parent_base, dict) and parent_base.get("role") in TABLE_ROLES:
                if isinstance(parent_entry, dict) and parent_entry.get("table_id") != tid:
                    violations.append(
                        f"{prefix} row table_id {tid!r} does not match parent table_id {parent_entry.get('table_id')!r}"
                    )

        if role in CELL_ROLES:
            tid = entry.get("table_id")
            rid = entry.get("row_id")
            if _is_nonempty_str(tid) and tid not in table_summaries:
                violations.append(f"{prefix} cell references unknown table_id {tid!r}")
            if _is_nonempty_str(tid) and _is_nonempty_str(rid) and (tid, rid) not in seen_row_keys:
                violations.append(f"{prefix} cell references unknown row ({tid!r}, {rid!r})")
            if isinstance(parent_base, dict) and parent_base.get("role") in ROW_ROLES:
                if isinstance(parent_entry, dict):
                    if parent_entry.get("table_id") != tid or parent_entry.get("row_id") != rid:
                        violations.append(
                            f"{prefix} cell linkage ({tid!r}, {rid!r}) mismatches parent row "
                            f"({parent_entry.get('table_id')!r}, {parent_entry.get('row_id')!r})"
                        )

    for tid, (tnid, tentry) in table_summaries.items():
        rows = table_rows_by_table.get(tid, [])
        vr = tentry.get("visible_range")
        if isinstance(vr, dict) and _is_int(vr.get("count")) and vr["count"] >= 0:
            cnt = vr["count"]
            if cnt == 0 and rows:
                violations.append(
                    f"sidecar.nodes[{tnid!r}].visible_range count is 0 but {len(rows)} row(s) are linked"
                )
            elif cnt > 0 and rows:
                if len(rows) != cnt:
                    violations.append(
                        f"sidecar.nodes[{tnid!r}].visible_range count {cnt} != linked row count {len(rows)}"
                    )
                if vr.get("first_row_id") != rows[0][1]:
                    violations.append(
                        f"sidecar.nodes[{tnid!r}].visible_range.first_row_id {vr.get('first_row_id')!r} "
                        f"!= first linked row {rows[0][1]!r}"
                    )
                if vr.get("last_row_id") != rows[-1][1]:
                    violations.append(
                        f"sidecar.nodes[{tnid!r}].visible_range.last_row_id {vr.get('last_row_id')!r} "
                        f"!= last linked row {rows[-1][1]!r}"
                    )

    return violations


def _validate_outbox_record_dict(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or not record:
        raise GodotError("outbox record must be a non-empty JSON object")
    _no_float_or_bad_types(record, "outbox_record")

    rec_schema = record.get("schema")
    if rec_schema == schema.SCHEMA_ID:
        errs = schema.validate_state(record)
        if errs:
            raise GodotError("invalid base state in outbox batch: " + "; ".join(errs))
        if record.get("source", {}).get("kind") != "godot":
            raise GodotError("outbox base state must have source.kind == 'godot'")
        return record

    if rec_schema == UITREE_SCHEMA_ID:
        if sorted(record.keys()) != sorted(SIDECAR_TOP_KEYS):
            raise GodotError("invalid sidecar keys in outbox batch")
        if not isinstance(record.get("base_sha256"), str) or not _HEX64_RE.match(record["base_sha256"]):
            raise GodotError("invalid sidecar base_sha256 in outbox batch")
        if not isinstance(record.get("nodes"), dict):
            raise GodotError("invalid sidecar nodes in outbox batch")
        for nid, entry in record["nodes"].items():
            if not isinstance(nid, str) or not _HEX16_RE.match(nid) or not isinstance(entry, dict):
                raise GodotError("invalid sidecar node entry in outbox batch")
            if set(entry.keys()) - SIDECAR_ALL_NODE_KEYS:
                raise GodotError("unknown fields in sidecar node entry in outbox batch")
            if not is_valid_control_path(entry.get("control_path")) or not _is_nonempty_str(entry.get("screen_id")):
                raise GodotError("invalid control_path or screen_id in outbox sidecar")
            if "/" in entry["control_path"] and entry["control_path"].split("/", 1)[0] != entry["screen_id"]:
                raise GodotError("control_path must be rooted at screen_id in outbox sidecar")
            if node_id_for_control_path(entry["control_path"]) != nid:
                raise GodotError("control_path hash mismatch in outbox sidecar")
        return record

    if rec_schema is not None:
        raise GodotError(f"unknown schema in outbox record: {rec_schema!r}")

    if "base" in record and "sidecar" in record:
        if set(record.keys()) - {"base", "sidecar", "tick", "ui_state"}:
            raise GodotError("unknown fields in combined outbox record")
        errs = validate_uitree(record["base"], record["sidecar"])
        if errs:
            raise GodotError("invalid base/sidecar pair in outbox batch: " + "; ".join(errs))

    # Engine-produced outcome records are distinct from client inbox commands.
    # order_seq_id is required here but stays forbidden in command arguments.
    is_outcome = (
        any(k in record for k in ("requested_tick", "applied_tick", "order_seq_id"))
        or (isinstance(record.get("status"), str)
            and record["status"] in OUTCOME_STATUSES
            and ("seq" in record or "state_sha256" in record))
    )
    if is_outcome:
        if set(record) != OUTCOME_RECORD_KEYS:
            raise GodotError("outcome record must have exactly the Holdings outcome keys")
        if (not _is_int(record["seq"]) or record["seq"] <= 0
                or not _is_int(record["order_seq_id"]) or record["order_seq_id"] <= 0):
            raise GodotError("outcome seq and order_seq_id must be positive integers")
        if (not _is_int(record["requested_tick"]) or record["requested_tick"] < 0
                or not _is_int(record["applied_tick"]) or record["applied_tick"] < 0):
            raise GodotError("outcome ticks must be non-negative integers")
        if not isinstance(record["status"], str) or record["status"] not in OUTCOME_STATUSES:
            raise GodotError("invalid Holdings outcome status")
        if record["reason"] is not None and (
            not _is_nonempty_str(record["reason"])
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", record["reason"])
        ):
            raise GodotError("invalid Holdings outcome reason code")
        if not isinstance(record["state_sha256"], str) or not _HEX64_RE.fullmatch(record["state_sha256"]):
            raise GodotError("invalid outcome state_sha256")
        return record

    if "cmd" in record or "args" in record:
        if sorted(record.keys()) != sorted(INBOX_RECORD_KEYS):
            raise GodotError("unknown or missing fields in command-shaped record")
    for k in record.keys():
        if k in FORBIDDEN_AUTHORITY_KEYS or k.startswith(("unknown", "extra", "unexpected", "bogus", "invalid")):
            raise GodotError(f"unknown or forbidden field {k!r} in outbox record")
    for int_field in ("tick", "as_of_tick", "count", "page_index"):
        if int_field in record and (not _is_int(record[int_field]) or record[int_field] < 0):
            raise GodotError(f"outbox record field {int_field!r} must be a non-negative integer")
    for pos_int_field in ("seq", "order_seq_id", "page_size"):
        if pos_int_field in record and (not _is_int(record[pos_int_field]) or record[pos_int_field] <= 0):
            raise GodotError(f"outbox record field {pos_int_field!r} must be a positive integer")

    schema.dumps(record)
    return record


def _normalize_decoded_batch(raw_batch: Any) -> list[dict[str, Any]]:
    if isinstance(raw_batch, dict):
        if raw_batch.get("complete") is False:
            raise GodotError("incomplete batch exposed by decoder")
        if "records" in raw_batch:
            raw_batch = raw_batch["records"]
        elif "lines" in raw_batch:
            raw_batch = raw_batch["lines"]
        else:
            raise GodotError("malformed batch dict from decoder")

    records_out: list[dict[str, Any]] = []
    if isinstance(raw_batch, (bytes, bytearray, memoryview, str)):
        if isinstance(raw_batch, (bytes, bytearray, memoryview)):
            raw_bytes = bytes(raw_batch)
            if b"\r" in raw_bytes:
                raise GodotError("CR line endings not allowed in canonical LF-delimited batch")
            try:
                text = raw_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise GodotError(f"invalid UTF-8 in batch payload: {exc}") from exc
        else:
            if "\r" in raw_batch:
                raise GodotError("CR line endings not allowed in canonical LF-delimited batch")
            _ensure_valid_utf8_str(raw_batch, "batch")
            text = raw_batch

        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if not lines:
            raise GodotError("empty outbox batch")
        for line in lines:
            if not line:
                raise GodotError("empty line inside outbox batch")
            rec = parse_canonical_json_line(line)
            records_out.append(_validate_outbox_record_dict(rec))
    elif isinstance(raw_batch, (list, tuple)):
        if not raw_batch:
            raise GodotError("empty outbox batch")
        for item in raw_batch:
            if isinstance(item, (bytes, bytearray, memoryview, str)):
                rec = parse_canonical_json_line(item)
                records_out.append(_validate_outbox_record_dict(rec))
            elif isinstance(item, dict):
                records_out.append(_validate_outbox_record_dict(item))
            else:
                raise GodotError(f"invalid item type in outbox batch: {type(item)!r}")
    else:
        raise GodotError(f"invalid batch type from decoder: {type(raw_batch)!r}")

    bases = [r for r in records_out if r.get("schema") == schema.SCHEMA_ID]
    sidecars = [r for r in records_out if r.get("schema") == UITREE_SCHEMA_ID]
    if bases and sidecars:
        if len(bases) != len(sidecars):
            raise GodotError("mismatched count of base and sidecar records in outbox batch")
        for b_rec, s_rec in zip(bases, sidecars):
            errs = validate_uitree(b_rec, s_rec)
            if errs:
                raise GodotError("invalid base/sidecar linkage in outbox batch: " + "; ".join(errs))

    return records_out


# @anchor:wa.perceive.godot.read_outbox_batches
def read_outbox_batches(
    data: bytes | bytearray | memoryview,
    decode_batches: Callable[[bytes], tuple[list[Any], bytes]],
) -> OutboxReadResult:
    """Decode complete outbox record batches and pending bytes via the required injected decoder."""
    if decode_batches is None or not callable(decode_batches):
        raise ValueError("decode_batches codec is required and must be callable")
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(data)
    elif isinstance(data, str):
        _ensure_valid_utf8_str(data, "data")
        raw_bytes = data.encode("utf-8")
    else:
        raise ValueError("data must be bytes-like")

    try:
        decoded = decode_batches(raw_bytes)
    except Exception as exc:
        raise ValueError(f"outbox batch decoding failed: {exc}") from exc

    if not isinstance(decoded, tuple) or len(decoded) != 2:
        raise ValueError("decode_batches must return a (batches, pending_bytes) 2-tuple")

    raw_batches, pending = decoded
    if not isinstance(raw_batches, (list, tuple)):
        raise ValueError("decoded batches must be a list or tuple")
    if isinstance(pending, (bytes, bytearray, memoryview)):
        pending_bytes = bytes(pending)
    else:
        raise ValueError("pending bytes must be bytes-like")

    validated_batches: list[list[dict[str, Any]]] = []
    for raw_batch in raw_batches:
        validated_batches.append(_normalize_decoded_batch(raw_batch))

    return OutboxReadResult(validated_batches, pending_bytes)


def validate_inbox_record(record: Any) -> dict[str, Any]:
    """Validate a single external command record before inbox encoding."""
    if isinstance(record, (bytes, bytearray, memoryview, str)):
        parsed = parse_canonical_json_line(record)
    elif isinstance(record, dict):
        parsed = record
    else:
        raise ValueError("inbox command record must be a dict or canonical JSON line")

    _no_float_or_bad_types(parsed, "command")

    if sorted(parsed.keys()) != sorted(INBOX_RECORD_KEYS):
        unknown = sorted(set(parsed.keys()) - set(INBOX_RECORD_KEYS))
        missing = sorted(set(INBOX_RECORD_KEYS) - set(parsed.keys()))
        raise ValueError(f"invalid command keys (unknown={unknown}, missing={missing})")

    tick = parsed["tick"]
    seq = parsed["seq"]
    cmd = parsed["cmd"]
    args = parsed["args"]

    if not _is_int(tick) or tick < 0:
        raise ValueError(f"tick must be a non-negative integer, got {tick!r}")
    if not _is_int(seq) or seq < 1:
        raise ValueError(f"seq must be a positive integer, got {seq!r}")
    if not _is_nonempty_str(cmd):
        raise ValueError(f"cmd must be a non-empty string, got {cmd!r}")
    if cmd in REJECTED_CANONICAL_ALIASES:
        raise ValueError(f"UI alias {cmd!r} is not a canonical command")
    if not isinstance(args, dict):
        raise ValueError("args must be a JSON object")

    _reject_forbidden_authority_fields(args, "command.args")

    canonical = {
        "args": args,
        "cmd": cmd,
        "seq": seq,
        "tick": tick,
    }
    schema.dumps(canonical)
    return canonical


def _reject_forbidden_authority_fields(obj: Any, path: str) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_AUTHORITY_KEYS:
                if k == "principal" and (_is_int(v) or path.endswith(".terms")):
                    continue
                raise ValueError(f"forbidden authority/consent field {k!r} at {path}")
            _reject_forbidden_authority_fields(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_forbidden_authority_fields(v, f"{path}[{i}]")


# @anchor:wa.perceive.godot.write_inbox_batch
def write_inbox_batch(
    records: list[dict[str, Any]],
    encode_batch: Callable[[Any], bytes],
    sink: Callable[[bytes], Any],
) -> bytes:
    """Validate all inbox command records before encoding and a single `sink` call."""
    if encode_batch is None or not callable(encode_batch):
        raise ValueError("encode_batch codec is required and must be callable")
    if sink is None or not callable(sink):
        raise ValueError("sink is required and must be callable")
    if not isinstance(records, (list, tuple)) or not records:
        raise ValueError("records must be a non-empty list or tuple")

    validated: list[dict[str, Any]] = []
    seen_seqs: set[int] = set()
    for rec in records:
        c_rec = validate_inbox_record(rec)
        if c_rec["seq"] in seen_seqs:
            raise ValueError(f"duplicate seq {c_rec['seq']} within inbox batch")
        seen_seqs.add(c_rec["seq"])
        validated.append(c_rec)

    canonical_lines = [schema.dumps(r) for r in validated]
    canonical_bytes = ("\n".join(canonical_lines) + "\n").encode("utf-8")

    try:
        encoded = encode_batch(validated)
    except (TypeError, AttributeError):
        try:
            encoded = encode_batch(canonical_lines)
        except (TypeError, AttributeError):
            encoded = encode_batch(canonical_bytes)

    if isinstance(encoded, str):
        _ensure_valid_utf8_str(encoded, "encoded_batch")
        encoded_bytes = encoded.encode("utf-8")
    elif isinstance(encoded, (bytes, bytearray, memoryview)):
        encoded_bytes = bytes(encoded)
    else:
        raise ValueError("encode_batch must return bytes or UTF-8 string")

    sink(encoded_bytes)
    return encoded_bytes


def _normalize_endpoint(ep: str | Path | None) -> str | None:
    if ep is None or ep == "":
        return None
    if isinstance(ep, Path):
        return str(ep.resolve())
    if isinstance(ep, str):
        if not ep.strip():
            raise ValueError("endpoint path cannot be whitespace-only")
        return str(Path(ep).resolve()) if ("/" in ep or "\\" in ep) else ep.strip()
    raise ValueError(f"invalid endpoint specification: {ep!r}")


# @anchor:wa.perceive.godot.GodotT2Client
class GodotT2Client:
    """Session-owned Godot T2 mailbox client supporting explicit enabled or OFF transport."""

    def __init__(
        self,
        t2_inbox: str | Path | None = None,
        t2_outbox: str | Path | None = None,
        publisher: str | Callable | None = None,
        *,
        decode_batches: Callable[[bytes], tuple[list[Any], bytes]] | None = None,
        encode_batch: Callable[[Any], bytes] | None = None,
        sink: Callable[[bytes], Any] | None = None,
    ) -> None:
        norm_in = _normalize_endpoint(t2_inbox)
        norm_out = _normalize_endpoint(t2_outbox)

        if norm_in is None and norm_out is None:
            self.enabled = False
            self.mode = "OFF"
            self.t2_inbox = None
            self.t2_outbox = None
            self.publisher = None
            self._decode_batches = decode_batches
            self._encode_batch = encode_batch
            self._sink = sink
            self._pending = b""
            return

        if norm_in is None or norm_out is None:
            raise ValueError("partial T2 mailbox endpoint pair refused: both t2-inbox and t2-outbox are required")
        if norm_in == norm_out:
            raise ValueError("coincident T2 mailbox endpoints refused: t2-inbox and t2-outbox must be distinct")
        if publisher is None or (isinstance(publisher, str) and not publisher.strip()):
            raise ValueError("missing publisher refused when T2 mailbox endpoints are enabled")
        if decode_batches is None or not callable(decode_batches):
            raise ValueError("missing decode_batches codec refused when T2 mailbox is enabled")
        if encode_batch is None or not callable(encode_batch):
            raise ValueError("missing encode_batch codec refused when T2 mailbox is enabled")

        self.enabled = True
        self.mode = "ENABLED"
        self.t2_inbox = str(t2_inbox)
        self.t2_outbox = str(t2_outbox)
        self.publisher = publisher
        self._decode_batches = decode_batches
        self._encode_batch = encode_batch
        self._sink = sink
        self._pending = b""

    def poll_outbox(self, data: bytes = b"") -> OutboxReadResult:
        """Read complete batches from `data` (or the session outbox file if enabled)."""
        if not self.enabled:
            return OutboxReadResult([], b"")
        chunk = bytes(data)
        if not chunk and self.t2_outbox and Path(self.t2_outbox).is_file():
            chunk = Path(self.t2_outbox).read_bytes()
            buf = chunk
        else:
            buf = self._pending + chunk
        res = read_outbox_batches(buf, self._decode_batches)
        self._pending = res.pending
        return res

    def send_inbox_batch(self, records: list[dict[str, Any]]) -> bytes | None:
        """Validate and write a command batch when enabled; OFF mode performs no writes."""
        if not self.enabled:
            return None
        target_sink = self._sink
        if target_sink is None:
            inbox_path = Path(self.t2_inbox)

            def _file_sink(payload: bytes) -> None:
                with inbox_path.open("ab") as f:
                    f.write(payload)

            target_sink = _file_sink
        return write_inbox_batch(records, self._encode_batch, target_sink)
