# @module {"id":"wa.perceive.element_table","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.ax","wa.perceive.srf_identity","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Deterministic RAM-only tag fusion. Contract: build/spec/w5_w.md, stages 1–7.

Names are passive data; executable producer identifiers remain in the private map.
The table state word is the vqa.element-table/v1 layout, not the AX wire layout.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import re
import unicodedata

from wender.perceive import ax, srf_identity
from wender.state.schema import _no_float

# @anchor:wa.perceive.element_table.SCHEMA_ID
SCHEMA_ID = "vqa.element-table/v1"
# @anchor:wa.perceive.element_table.MAX_TABLE_ROWS
MAX_TABLE_ROWS = 30
# @anchor:wa.perceive.element_table.MAX_TABLE_TOKENS
MAX_TABLE_TOKENS = 1200
# @anchor:wa.perceive.element_table.MAX_SEB_OPTIONS
MAX_SEB_OPTIONS = 20
# @anchor:wa.perceive.element_table.MAX_RECENCY_MATCHED
MAX_RECENCY_MATCHED = 15
# @anchor:wa.perceive.element_table.ABSTAIN_TEXT
ABSTAIN_TEXT = "No safely grounded visible option; hand off for reasoning."
# @anchor:wa.perceive.element_table.SENTINEL_ZERO_SHA256
SENTINEL_ZERO_SHA256 = "0" * 64
# @anchor:wa.perceive.element_table.BIT_BUSY
BIT_BUSY = 1 << 0
# @anchor:wa.perceive.element_table.BIT_CHECKED
BIT_CHECKED = 1 << 1
# @anchor:wa.perceive.element_table.BIT_CURRENT
BIT_CURRENT = 1 << 2
# @anchor:wa.perceive.element_table.BIT_DISABLED
BIT_DISABLED = 1 << 3
# @anchor:wa.perceive.element_table.BIT_EXPANDED
BIT_EXPANDED = 1 << 4
# @anchor:wa.perceive.element_table.BIT_HIDDEN
BIT_HIDDEN = 1 << 5
# @anchor:wa.perceive.element_table.BIT_PRESSED
BIT_PRESSED = 1 << 6
# @anchor:wa.perceive.element_table.BIT_SELECTED
BIT_SELECTED = 1 << 7
# @anchor:wa.perceive.element_table.BIT_VISIBLE
BIT_VISIBLE = 1 << 8
# @anchor:wa.perceive.element_table.BIT_FOCUSED
BIT_FOCUSED = 1 << 9
# @anchor:wa.perceive.element_table.BIT_READ
BIT_READ = 1 << 10
# @anchor:wa.perceive.element_table.BIT_ACKED
BIT_ACKED = 1 << 11
# @anchor:wa.perceive.element_table.M_ACT
M_ACT = BIT_VISIBLE
# @anchor:wa.perceive.element_table.REGION_RANK
REGION_RANK = {"composer": 0, "answer:last": 1, "main": 2, "dialog": 3,
               "menu": 4, "header": 5, "toolbar": 6, "nav": 7,
               "navigation": 7, "sidebar": 8, "footer": 9}
_FLAGS = (*ax.STATE_ATTRS, "visible", "focused", "read", "acked")
_INPUTS = frozenset(("textbox", "searchbox", "combobox", "input", "textarea", "password"))
_ACTIONS = frozenset(("click", "type", "key", "scroll", "select", "toggle", "focus"))


# @anchor:wa.perceive.element_table.ProvenanceRecord
@dataclass(frozen=True)
class ProvenanceRecord:
    source: str
    ref: str | None = None
    revision: int | None = None
    method: str = "direct"
    raw_name: str | None = None
    raw_scale: str = "int"
    cal_id: str | None = None
    conflicts: tuple[str, ...] = ()
    competitors: tuple[str, ...] = ()
    missing_reason: str | None = None


# @anchor:wa.perceive.element_table.ElementRow
@dataclass(frozen=True)
class ElementRow:
    option_id: str
    stable_id: str | None
    mark_id: int | None
    match_level: str
    role: str
    accessible_name: str | None
    canon_name: str | None
    visible_text: str | None
    value_len: int | None
    ocr_text: str | None
    ocr_score_promille: int | None
    region_landmark: str
    container_scope: str | None
    row_col_header: dict | None
    ordinal_of_n: dict
    wl_sig: str | None
    state_word: int
    state_flags: dict
    action_kind: tuple[str, ...]
    bbox_px: tuple[int, int, int, int] | None
    center_px: tuple[int, int] | None
    join_evidence: dict | None
    change_tag: dict | None
    provenance: dict
    missing: dict
    priority_tier: int
    rendered_line: str = ""


# @anchor:wa.perceive.element_table.InventoryReceipt
@dataclass(frozen=True)
class InventoryReceipt:
    total_nodes: int
    eligible_nodes: int
    retained_options: int
    omitted_count: int
    marks_count: int
    unmatched_marks: int
    sidebar_hidden: int
    blocked_control_present: int
    tokenizer_id: str
    head_tokens: int
    truncated: bool
    downgraded_count: int
    collision_unresolved: bool
    seb_omitted_count: int = 0


# @anchor:wa.perceive.element_table.ElementTable
@dataclass(frozen=True)
class ElementTable:
    schema: str
    rows: tuple[ElementRow, ...]
    sorted_rows: tuple[ElementRow, ...]
    inventory_receipt: InventoryReceipt
    image_worthy: bool
    seb_options: dict[str, str]
    labelled_options: dict[str, str]
    nodes_options: dict[str, str]
    _target_map: dict[str, dict]

    # @anchor:wa.perceive.element_table.ElementTable.inventory_receipt_dict
    def inventory_receipt_dict(self):
        return asdict(self.inventory_receipt)

    # @anchor:wa.perceive.element_table.ElementTable.recency_rows
    def recency_rows(self):
        return tuple(r for r in self.rows if r.priority_tier == 0)[-MAX_RECENCY_MATCHED:]

    # @anchor:wa.perceive.element_table.ElementTable.to_wire_dict
    def to_wire_dict(self):
        result = {"schema": self.schema, "rows": [asdict(r) for r in self.rows],
                  "inventory_receipt": self.inventory_receipt_dict(),
                  "image_worthy": self.image_worthy, "seb_options": dict(self.seb_options),
                  "labelled_options": dict(self.labelled_options), "nodes_options": dict(self.nodes_options)}
        _no_float(result)
        return result


# @anchor:wa.perceive.element_table.count_tokens
def count_tokens(text, tokenizer=None):
    """Exact consumer count, or a conservative UTF-8 byte bound without one."""
    return len(tokenizer.encode(text)) if tokenizer is not None else len(text.encode("utf-8"))


# @anchor:wa.perceive.element_table.validate_table_budget
def validate_table_budget(table, tokenizer=None, *, max_rows=MAX_TABLE_ROWS,
                          max_tokens=MAX_TABLE_TOKENS):
    rows = table.rows if isinstance(table, ElementTable) else table
    text = "\n".join(r.rendered_line for r in rows)
    if len(rows) > max_rows or count_tokens(text, tokenizer) > max_tokens:
        raise ValueError("table_budget_exceeded")
    return True


def _sort(row):
    box = row.bbox_px
    return (REGION_RANK.get(row.region_landmark, 5),
            box[1] if box else float("inf"), box[0] if box else float("inf"),
            row.stable_id or "", row.option_id)


def _priority(row):
    return (row.priority_tier, *_sort(row))


# @anchor:wa.perceive.element_table.validate_seb_key_map
def validate_seb_key_map(options, tokenizer=None, *, max_seb_options=MAX_SEB_OPTIONS):
    if type(max_seb_options) is not int or not 1 <= max_seb_options <= MAX_SEB_OPTIONS or len(options) > max_seb_options:
        raise ValueError("seb_choice_limit_exceeded")
    if any(not isinstance(k, str) or len(k) != 1 or not "A" <= k <= "T"
           or (tokenizer is not None and len(tokenizer.encode(k)) != 1) for k in options):
        raise ValueError("seb_requires_one_token")
    keys = sorted(options)
    if (not keys or keys != [chr(65 + i) for i in range(len(keys))]
            or options[keys[-1]] != ABSTAIN_TEXT):
        raise ValueError("seb_missing_terminal_abstain_key")
    return True


# @anchor:wa.perceive.element_table.build_seb_key_map
def build_seb_key_map(candidates, *, allow_preselect=False, tokenizer=None,
                      max_seb_options=MAX_SEB_OPTIONS):
    if type(max_seb_options) is not int or not 1 <= max_seb_options <= MAX_SEB_OPTIONS:
        raise ValueError("seb_choice_limit_exceeded")
    rows = list(candidates)
    if len(rows) >= max_seb_options:
        if not allow_preselect:
            raise ValueError("seb_choice_limit_exceeded")
        rows = sorted(rows, key=_priority)[:max_seb_options - 1]
    options = {chr(65 + i): r.rendered_line.partition(": ")[2]
               for i, r in enumerate(sorted(rows, key=_sort))}
    options[chr(65 + len(rows))] = ABSTAIN_TEXT
    validate_seb_key_map(options, tokenizer, max_seb_options=max_seb_options)
    return options


def _clean(value):
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r'https?://\S+|\bsk-[\w-]+', '[REDACTED]', text)
    text = re.sub(r'(?i)\b(?:bearer\s+|(?:token|secret|password|api[_-]?key)\s*[=:]\s*)[^\s,;]+',
                  '[REDACTED]', text)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()[:120]


def _box(e):
    b = e.get("bbox_px", e.get("bbox", e.get("box")))
    if b is None and all(k in e for k in ("x", "y", "w", "h")):
        b = tuple(e[k] for k in ("x", "y", "w", "h"))
    if isinstance(b, dict):
        b = tuple(b.get(k) for k in ("x", "y", "w", "h"))
    if b is None:
        return None
    if (not isinstance(b, (list, tuple)) or len(b) != 4
            or any(type(v) is not int for v in b) or min(b[2:]) <= 0
            or b[0] + b[2] <= 0 or b[1] + b[3] <= 0):
        raise ValueError("invalid_integer_geometry")
    return tuple(b)


def _reject_blend(value):
    if isinstance(value, dict):
        if isinstance(value.get("confidence"), float):
            raise ValueError("blended_confidence_prohibited")
        for k, v in value.items():
            if k not in ("raw_ax", "value"):
                _reject_blend(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _reject_blend(v)


def _state(e):
    states = {**(e.get("states") or {}), **(e.get("state_flags") or {})}
    if "state_word" in e:
        # AX nodes carry props; their bit layout is versioned independently.
        if "props" in e:
            states = {**ax.decode_state_word(e["state_word"]), **states}
        else:
            word = e["state_word"]
            if type(word) is not int or not 0 <= word <= 0xFFF:
                raise ValueError("invalid_state_word")
            states = {**{k: bool(word & (1 << i)) for i, k in enumerate(_FLAGS)}, **states}
    props = e.get("props") or {}
    for key, value in props.items():
        if key in _FLAGS and key not in states:
            states[key] = value in (True, "true", "mixed")
    for key in _FLAGS:
        if type(e.get(key)) is bool:
            states[key] = e[key]
    states.setdefault("visible", e.get("visible", True))
    if states.get("enabled") is False or e.get("enabled") is False:
        states["disabled"] = True
    flags = {k: states.get(k) if type(states.get(k)) is bool else None for k in _FLAGS}
    word = sum(1 << i for i, k in enumerate(_FLAGS) if flags[k] is True)
    return word, flags


def _iou(a, b):
    intersection = max(0, min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0])) * max(
        0, min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]))
    return intersection, a[2]*a[3] + b[2]*b[3] - intersection


def _render(row, *, tail=True, collision=False):
    if row.match_level == "VISUAL_ONLY":
        head = f"[M{row.mark_id}] {row.role} AUSENTE"
    else:
        head = f'{row.role} {json.dumps(row.accessible_name or "AUSENTE", ensure_ascii=False)}'
        if collision:
            # Put ordinal before long scope/name suffixes when the head gate needs it.
            ordinal = row.ordinal_of_n
            head += f' [{ordinal["k"]}-of-{ordinal["n"]}]'
            if row.container_scope:
                head += " scope=" + row.container_scope
            if row.row_col_header:
                head += " " + " ".join(f"{k}={v}" for k, v in sorted(row.row_col_header.items()))
            head += "".join(f" [{k}]" for k, v in row.state_flags.items() if v is True and k != "visible")
            head += " wl=" + str(row.wl_sig)
        head += " " + "/".join(row.action_kind)
    head += (f" @({row.center_px[0]},{row.center_px[1]})" if row.center_px else " AUSENTE")
    if tail and row.bbox_px:
        head += " [bbox=" + ",".join(map(str, row.bbox_px)) + "]"
    if tail and row.change_tag:
        head += " " + json.dumps(row.change_tag, sort_keys=True, separators=(",", ":"))
    return row.option_id + ": " + head


# @anchor:wa.perceive.element_table.assemble
def assemble(elements=(), marks=(), *, goal="", describe=None, join_map=None,
             disambig_map=None, tokenizer=None, tokenizer_id="byte_bound_v1",
             max_rows=MAX_TABLE_ROWS, max_tokens=MAX_TABLE_TOKENS,
             max_seb_options=MAX_SEB_OPTIONS):
    """Fuse safe nodes and marks, fit complete lines, and project consumer keys."""
    if (type(max_rows) is not int or not 0 <= max_rows <= MAX_TABLE_ROWS
            or type(max_tokens) is not int or not 0 <= max_tokens <= MAX_TABLE_TOKENS):
        raise ValueError("invalid_table_budget")
    if type(max_seb_options) is not int or not 1 <= max_seb_options <= MAX_SEB_OPTIONS:
        raise ValueError("seb_choice_limit_exceeded")
    elements, marks = list(elements), list(marks)
    _reject_blend((elements, marks))
    if marks and any((describe or {}).get(k) == SENTINEL_ZERO_SHA256
                     for k in ("sha256_rgb24", "frame_sha256")):
        raise ValueError("zero_digest_pixel_join_refused")
    rows, targets, anchors = [], {}, {}
    hidden = blocked = 0
    goal_words = set(re.findall(r"\w+", _clean(goal).casefold()))
    for e in elements:
        name = _clean(e.get("accessible_name", e.get("name", "")))
        role = str(e.get("role") or "visual_unknown")
        region = _clean(e.get("region_landmark", e.get("region", "main")))
        if region in ("navigation", "sidebar", "nav") and not srf_identity._NEW.fullmatch(name):
            hidden += 1
            continue
        if srf_identity._BLOCKED.search(name):
            blocked += 1
            continue
        word, flags = _state(e)
        if (not word & M_ACT or word & (BIT_DISABLED | BIT_HIDDEN)
                or e.get("actionable") is False):
            continue
        canon = re.sub(r"\b(investigando)\s+\d+\s*s\b", r"\1 <n>", name.casefold())
        if "investigando <n>" in canon:
            word |= BIT_BUSY
            flags["busy"] = True
        ordinal = e.get("ordinal", 0)
        if type(ordinal) is not int or ordinal < 0:
            raise ValueError("invalid_ordinal")
        stable = e.get("stable_id") or e.get("id")
        if not isinstance(stable, str) or not re.fullmatch(r"[a-fA-F0-9]{16}", stable):
            stable = hashlib.sha256(f"{role}\0{canon}\0{ordinal}".encode()).hexdigest()[:16]
        anchors[stable] = e.get("anchor_id")
        box = _box(e)
        center = (box[0] + box[2]//2, box[1] + box[3]//2) if box else None
        change = e.get("change_tag")
        if change is not None:
            _no_float(change)
            change = {k: (_clean(v) if isinstance(v, str) else v) for k, v in change.items()
                      if k in ("cls", "kind", "mv", "edge_ppm", "novelty")}
        value_len = e.get("value_len")
        if role in _INPUTS and value_len is None and "value" in e:
            value_len = len(str(e["value"]).encode("utf-16-le")) // 2
        if type(value_len) is not int or value_len < 0 or role not in _INPUTS:
            value_len = None
        scope = e.get("container_scope")
        if scope is None and isinstance(e.get("container"), dict):
            number = e["container"].get("ordinal")
            if type(number) is int:
                scope = f"container:{number}_actions"
        scope = _clean(scope) if scope is not None else None
        structure = (disambig_map or {}).get(stable, {})
        scope = _clean(structure.get("container_scope", scope)) if structure.get("container_scope", scope) is not None else None
        headers = structure.get("row_col_header", e.get("row_col_header"))
        if headers:
            headers = {k: (v if type(v) is int else _clean(v)) for k, v in headers.items()
                       if k in ("row", "col", "row_key", "header")}
        actions = tuple(sorted(set(e.get("action_kind") or (("type", "focus") if role in _INPUTS else ("click",))) & _ACTIONS))
        priority = (0 if region in ("composer", "answer:last") or goal_words & set(re.findall(r"\w+", canon))
                    else 1 if region == "main" else 2)
        option = _clean(e.get("id", stable))
        missing = {k: reason for k, reason in (("ocr_text", "ocr_absent"),
                   ("bbox_px", "no_geometry"), ("accessible_name", "name_absent"))
                   if k == "ocr_text" or (k == "bbox_px" and box is None) or (k == "accessible_name" and not name)}
        row = ElementRow(option, stable, None, "SEMANTIC_ONLY", role, name or None,
                         canon or None, None if role in _INPUTS else (_clean(e["visible_text"]) if e.get("visible_text") else None),
                         value_len, None, None, region, scope, headers, {}, None,
                         word, flags, actions, box, center, None, change, {}, missing, priority)
        rows.append(row)
        # The private map may retain backend handles but never raw content/values.
        targets[id(row)] = {k: e[k] for k in ("id", "backend_id", "backendDOMNodeId", "bbox", "box") if k in e}
    rows.sort(key=_sort)
    # Replace possibly sensitive producer ids with deterministic frame-local handles.
    for i, row in enumerate(rows):
        replacement = replace(row, option_id="e" + str(i))
        targets[id(replacement)] = targets.pop(id(row))
        rows[i] = replacement
    used = set()
    mark_ids = set()
    unmatched = 0
    for m in sorted(marks, key=lambda m: m["id"]):
        mid = m.get("id")
        if type(mid) is not int or not 1 <= mid <= 24 or mid in mark_ids:
            raise ValueError("invalid_mark_id")
        mark_ids.add(mid)
        box = _box(m)
        if box is None:
            raise ValueError("mark_geometry_missing")
        supplied = (join_map or {}).get(mid, (join_map or {}).get(str(mid)))
        choices = []
        for i, row in enumerate(rows):
            if i in used or row.bbox_px is None or row.stable_id is None:
                continue
            num, den = _iou(box, row.bbox_px)
            if supplied is not None:
                sid = supplied.get("stable_id") if isinstance(supplied, dict) else supplied
                if row.stable_id != sid:
                    continue
            if 2*num >= den:
                choices.append((-num * 1000000 // den, row.stable_id, i, num, den))
        if choices:
            _, _, i, num, den = min(choices)
            row = rows[i]
            used.add(i)
            evidence = {"verdict": "matched", "iou_num": num, "iou_den": den,
                        "w_promille": num*1000//den}
            if type(m.get("score")) in (int, float):
                score = m["score"]
                if not 0 <= score <= 1:
                    raise ValueError("invalid_rim_score")
                evidence["rim_promille"] = int(round(score*1000))
            level = supplied.get("match_level", "L0_EXACT") if isinstance(supplied, dict) else "L0_EXACT"
            if level not in ax.MATCH_LEVELS:
                raise ValueError("invalid_match_level")
            replacement = replace(row, mark_id=mid, match_level=level, join_evidence=evidence)
            targets[id(replacement)] = {**targets.pop(id(row)), "mark_id": mid}
            rows[i] = replacement
        else:
            unmatched += 1
            role = "visual_button" if m.get("kind") == "button" else "visual_unknown"
            priority = 0 if goal_words & {"this", "that", "aquí", "here", "visual"} else 3
            row = ElementRow(f"M{mid}", None, mid, "VISUAL_ONLY", role, None, None, None, None,
                             None, None, "main", None, None, {}, None, BIT_VISIBLE,
                             {k: (True if k == "visible" else None) for k in _FLAGS}, ("click",), box,
                             (box[0]+box[2]//2, box[1]+box[3]//2), None, None, {},
                             {"accessible_name": "unmatched_visual_mark", "stable_id": "unmatched_visual_mark",
                              "ocr_text": "ocr_absent"}, priority)
            ocr = m.get("ocr_text")
            if ocr is not None:
                score = m.get("ocr_score_promille")
                if score is not None and (type(score) is not int or not 0 <= score <= 1000):
                    raise ValueError("invalid_ocr_score")
                row = replace(row, ocr_text=_clean(ocr), ocr_score_promille=score,
                              missing={k: v for k, v in row.missing.items() if k != "ocr_text"})
            rows.append(row)
            targets[id(row)] = {"mark_id": mid}
    # Collision sets include duplicate labels, bare grid digits and closed parent overlap.
    groups = []
    for i, row in enumerate(rows):
        group = {j for j, other in enumerate(rows) if row.role == other.role and row.canon_name == other.canon_name
                 and row.canon_name is not None}
        if row.role == "gridcell" and (row.canon_name or "").isdigit():
            group |= {i}
        for j, other in enumerate(rows):
            if i == j or not row.canon_name or not other.canon_name:
                continue
            a1, a2 = anchors.get(row.stable_id), anchors.get(other.stable_id)
            if isinstance(a1, str) and isinstance(a2, str) and (a1.startswith(a2 + ".") or a2.startswith(a1 + ".")):
                group |= {i, j}
            if row.canon_name in other.canon_name or other.canon_name in row.canon_name:
                if row.bbox_px and other.bbox_px:
                    num, _ = _iou(row.bbox_px, other.bbox_px)
                    a, b = row.bbox_px[2]*row.bbox_px[3], other.bbox_px[2]*other.bbox_px[3]
                    if max(a, b) >= 2*min(a, b) and num*1000 >= 850*min(a, b):
                        group |= {i, j}
        if len(group) >= 2 or (row.role == "gridcell" and (row.canon_name or "").isdigit()):
            groups.append(group)
    merged = []
    for group in groups:
        overlapping = [g for g in merged if g & group]
        for g in overlapping:
            group |= g
            merged.remove(g)
        merged.append(group)
    colliding = set().union(*merged) if merged else set()
    for group in merged:
        for k, i in enumerate(sorted(group, key=lambda i: _sort(rows[i])), 1):
            row = rows[i]
            wl = hashlib.sha256(json.dumps([row.role, row.canon_name, row.container_scope,
                                           row.row_col_header, k], sort_keys=True).encode()).hexdigest()[:8]
            replacement = replace(row, ordinal_of_n={"k": k, "n": len(group), "tag": f"{k}-of-{len(group)}"}, wl_sig=wl)
            targets[id(replacement)] = targets.pop(id(row))
            rows[i] = replacement
    unresolved = False
    rendered = []
    for i, row in enumerate(rows):
        line = _render(row, collision=i in colliding)
        replacement = replace(row, rendered_line=line)
        targets[id(replacement)] = targets.pop(id(row))
        rendered.append(replacement)
    for group in merged:
        limits = (48, 8)
        def prefix(index, limit):
            head = rendered[index].rendered_line.partition(": ")[2]
            return tuple((tokenizer.encode(head) if tokenizer else head.encode())[:limit])
        if any(len({prefix(i, limit) for i in group}) != len(group) for limit in limits):
            for i in group:
                row = rendered[i]
                line = row.option_id + f': [{row.ordinal_of_n["k"]}-of-{row.ordinal_of_n["n"]}] ' + row.rendered_line.partition(": ")[2]
                replacement = replace(row, rendered_line=line)
                targets[id(replacement)] = targets.pop(id(row))
                rendered[i] = replacement
        unresolved |= any(len({prefix(i, limit) for i in group}) != len(group) for limit in limits)
    rows = sorted(rendered, key=_priority)
    eligible = len(rows)
    downgraded = 0
    def cost():
        return count_tokens("\n".join(r.rendered_line for r in rows), tokenizer)
    for i in range(len(rows)-1, -1, -1):
        if cost() <= max_tokens:
            break
        row = rows[i]
        # Remove optional tail only; never slice a rendered line.
        line = row.rendered_line.split(" [bbox=", 1)[0]
        if line != row.rendered_line:
            replacement = replace(row, rendered_line=line)
            targets[id(replacement)] = targets.pop(id(row))
            rows[i] = replacement
            downgraded += 1
    while rows and (len(rows) > max_rows or cost() > max_tokens):
        rows.pop()
    canonical = tuple(sorted(rows, key=_sort))
    selected = sorted(sorted(rows, key=_priority)[:max_seb_options-1], key=_sort)
    seb = build_seb_key_map([] if unresolved else selected, tokenizer=tokenizer,
                            max_seb_options=max_seb_options)
    target_map = {r.option_id: targets[id(r)] for r in canonical}
    if not unresolved:
        target_map.update({chr(65+i): targets[id(r)] for i, r in enumerate(selected)})
    recency = sorted((r for r in rows if r.priority_tier == 0), key=_sort)[:MAX_RECENCY_MATCHED]
    recency_ids = {id(r) for r in recency}
    ordered = tuple([r for r in canonical if id(r) not in recency_ids] + recency)
    receipt = InventoryReceipt(len(elements), eligible, len(rows), eligible-len(rows), len(marks),
                               unmatched, hidden, blocked,
                               tokenizer_id if tokenizer is not None else "byte_bound_v1",
                               count_tokens("\n".join(r.rendered_line for r in ordered), tokenizer),
                               len(rows) < eligible, downgraded, unresolved,
                               len(rows) if unresolved else len(rows)-len(selected))
    # Attach per-field provenance after fitting, so it cannot alter token admission.
    def provenance(row):
        fields = asdict(row)
        missing = {k: row.missing.get(k, "not_supplied") for k, v in fields.items() if v is None}
        missing.update(row.missing)
        records = {k: asdict(ProvenanceRecord(
            "vision_engine" if k in ("mark_id", "bbox_px", "center_px") and row.match_level == "VISUAL_ONLY" else
            "wender" if k in ("option_id", "stable_id", "canon_name", "ordinal_of_n", "wl_sig", "rendered_line") else "ax",
            missing_reason=missing.get(k))) for k in fields if k not in ("provenance", "missing")}
        if row.join_evidence is not None:
            records["join_evidence"] = asdict(ProvenanceRecord("wender", method="integer_iou", raw_scale="ratio"))
            if "rim_promille" in row.join_evidence:
                records["rim_promille"] = asdict(ProvenanceRecord("vision_engine", raw_scale="promille"))
        for k in ("ocr_text", "ocr_score_promille"):
            records[k] = asdict(ProvenanceRecord("ocr", raw_scale="promille" if k.endswith("promille") else "text",
                                                missing_reason=missing.get(k)))
        return replace(row, provenance=records, missing=missing)
    final_rows = {r.option_id: provenance(r) for r in canonical}
    table = ElementTable(SCHEMA_ID, tuple(final_rows[r.option_id] for r in ordered),
                         tuple(final_rows[r.option_id] for r in canonical), receipt, bool(marks), seb,
                         {r.option_id: r.rendered_line for r in canonical},
                         {r.option_id: r.rendered_line for r in canonical if r.match_level != "VISUAL_ONLY"}, target_map)
    validate_table_budget(table, tokenizer, max_rows=max_rows, max_tokens=max_tokens)
    return table


@dataclass(frozen=True)
class _RawElementEvidence:
    element_id: int
    role: str | None
    role_code: int | None
    ax_state_word: int | None
    explicit_visible: bool | None
    explicit_enabled: bool | None
    explicit_actionable: bool | None
    explicit_focused: bool | None
    explicit_value_nonempty: bool | None
    gen16: int | None
    backend_node_id: int | None
    mark_id: int | None
    match_level: str | None
    join_iou: float | None
    join_center_in_box: bool | None
    bbox: tuple | None
    click_point: tuple | None
    landmark_role: str | None
    container_key: str | None
    raw_label: str | None
    label_source: str | None


def _extract_raw_script_evidence(raw_elements, *, join_map=None, ax_matches=None):
    """Read producer evidence before presentation defaults are applied."""
    out, seen = [], set()
    for e in raw_elements:
        if not isinstance(e, dict):
            raise ValueError('G-FACTS: requires unprojected producer elements')
        eid = e.get('element_id', e.get('id'))
        if type(eid) is not int or eid < 0 or eid in seen:
            raise ValueError('invalid or duplicate element id')
        seen.add(eid)
        word = e.get('ax_state_word')
        if word is None and e.get('wire_schema') == 'ax_wire_v1':
            word = e.get('state_word')
        if type(word) is not int or not 0 <= word <= 65535:
            word = None
        states = e.get('states') or {}
        def fact(name, mask):
            if word is not None:
                return bool(word & mask)
            value = e[name] if name in e else states.get(name)
            return value if type(value) is bool else None
        def integer(*keys):
            for key in keys:
                if type(e.get(key)) is int:
                    return e[key]
            return None
        def geometry(key, count):
            value = e.get(key)
            if (isinstance(value, (tuple, list)) and len(value) == count
                    and all(type(v) is int for v in value)
                    and (count != 4 or value[2] > 0 and value[3] > 0)):
                return tuple(value)
            return None
        gen = integer('gen16', 'generation')
        if gen is not None and not 0 <= gen <= 65535:
            gen = None
        join = (join_map or {}).get(eid, {})
        match = (ax_matches or {}).get(eid)
        level = match.get('level') if isinstance(match, dict) else getattr(match, 'level', None)
        level = level if level is not None else join.get('match_level')
        out.append(_RawElementEvidence(eid, e.get('role'), integer('role_code'), word,
            fact('visible', 1 << 10), fact('enabled', 1 << 11), fact('actionable', 1 << 9),
            fact('focused', 1 << 8), fact('value_nonempty', 1 << 12), gen,
            integer('backend_id', 'backendDOMNodeId', 'backend_node_id'),
            join.get('mark_id'), level, join.get('iou'), join.get('center_in_box'),
            geometry('bbox', 4), geometry('click_point', 2), e.get('landmark_role'),
            e.get('container_key', e.get('container_id', e.get('landmark_id'))),
            e.get('label', e.get('name')), e.get('label_source', e.get('classification'))))
    return tuple(out)
