# @module {"id":"wa.state.schema","layer":0,"owner":"STATE","deps":["wa.state.handles"],"book":"HB-WA-a0-arch"}
"""@module wa.state.schema - the deterministic JSON STATE (`wender.state/v1`): build, validate, dump.

The STATE is what reaches a model instead of raw AX/HTML. It is canonical JSON (sorted keys,
compact separators, integers only, no floats), so equal pages produce equal bytes.
Machine schema: wender/state/wender.state.v1.schema.json. Human doc: docs/STATE.md.
"""
import dataclasses
import json
import re

SCHEMA_ID = "wender.state/v1"
SOURCE_KINDS = ("cdp", "atspi", "godot", "vision", "blender")
_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
NODE_KEYS = ("actionable", "box", "depth", "id", "name", "ordinal", "parent", "role", "states")
TOP_KEYS = ("actionable", "ax_sha256", "ax_stable_sha256", "counts", "nodes", "prune", "schema",
            "source", "viewport")

TARGET_KINDS = ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")
TARGET_DESCRIPTOR_KEYS = ("dpr_ppm", "epoch", "frame_seq", "handle", "state")
HANDLE_KEYS = ("canonical_id", "generation", "kind")

METADATA_LOCK_ORDER = (
    "scheduler_or_fence",
    "registry",
    "budget",
    "target_actor_metadata",
    "focus_domain",
    "stream_bank",
    "per_stream_metadata",
)

# @anchor:wa.state.schema.StateError
class StateError(ValueError):
    pass


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


from wender.state.handles import (
    Handle, TargetRecord, FrameRef, Observation, Receipt, ActionRequest,
    ActorLease, FocusLease, StreamSpec, StreamToken, Ticket, ChangeResult,
    PerceptionResult, WindowFrames, WindowDescription,
)


def _no_float(o, path="$"):
    if isinstance(o, float):
        raise StateError("float at %s" % path)
    if isinstance(o, dict):
        for k, v in o.items():
            _no_float(k, path + ".<key>")
            _no_float(v, path + "." + str(k))
    elif isinstance(o, (list, tuple)):
        for i, v in enumerate(o):
            _no_float(v, "%s[%d]" % (path, i))
    elif dataclasses.is_dataclass(o) and not isinstance(o, type):
        for f in dataclasses.fields(o):
            _no_float(getattr(o, f.name), path + "." + f.name)


def _json_default(o):
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


# @anchor:wa.state.schema.dumps
def dumps(obj):
    """Canonical JSON. Floats are refused (integers only)."""
    _no_float(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


# @anchor:wa.state.schema.build_state
def build_state(nodes, *, kind, url, title, ax_sha256, ax_stable_sha256, prune, raw_count,
                boxes=None, viewport=None, actionable_roles=frozenset()):
    """Assemble a STATE from pruned nodes (wender.perceive.ax.prune output).

    `boxes` maps node id -> [x, y, w, h] integer CSS px (not hashed: layout-dependent).
    `parent` is the id of the nearest emitted ancestor (null for roots).
    """
    boxes = boxes or {}
    out_nodes = []
    stack = []  # (depth, id) of the open emitted ancestors
    for n in nodes:
        while stack and stack[-1][0] >= n["depth"]:
            stack.pop()
        parent = stack[-1][1] if stack else None
        stack.append((n["depth"], n["id"]))
        out_nodes.append({
            "id": n["id"],
            "role": n["role"],
            "name": n["name"],
            "depth": n["depth"],
            "parent": parent,
            "ordinal": n["ordinal"],
            "states": {k: v for k, v in n["attrs"]},
            "actionable": n["role"] in actionable_roles and dict(n["attrs"]).get("disabled") != "true",
            "box": boxes.get(n["id"]),
        })
    state = {
        "schema": SCHEMA_ID,
        "source": {"kind": kind, "url": url, "title": title},
        "prune": prune,
        "ax_sha256": ax_sha256,
        "ax_stable_sha256": ax_stable_sha256,
        "viewport": viewport,
        "nodes": out_nodes,
        "actionable": [n["id"] for n in out_nodes if n["actionable"]],
        "counts": {"raw": raw_count, "nodes": len(out_nodes),
                   "actionable": sum(1 for n in out_nodes if n["actionable"])},
    }
    errs = validate_state(state)
    if errs:
        raise StateError("; ".join(errs))
    return state


# @anchor:wa.state.schema.validate_state
def validate_state(s):
    """Return a list of violations (empty = valid). Stdlib only, mirrors the JSON schema."""
    e = []
    if not isinstance(s, dict):
        return ["state is not an object"]
    if sorted(s) != sorted(TOP_KEYS):
        e.append("top-level keys %s != %s" % (sorted(s), sorted(TOP_KEYS)))
        return e
    if s["schema"] != SCHEMA_ID:
        e.append("schema must be %s" % SCHEMA_ID)
    src = s["source"]
    if not isinstance(src, dict) or src.get("kind") not in SOURCE_KINDS:
        e.append("source.kind must be one of %s" % (SOURCE_KINDS,))
    for k in ("ax_sha256", "ax_stable_sha256"):
        if not isinstance(s[k], str) or not _SHA_RE.match(s[k]):
            e.append("%s must be 64 hex" % k)
    vp = s["viewport"]
    if vp is not None and (not isinstance(vp, dict) or not all(_is_int(v) for v in vp.values())):
        e.append("viewport must be null or an object of integers")
    ids = set()
    for i, n in enumerate(s["nodes"]):
        p = "nodes[%d]" % i
        if not isinstance(n, dict) or sorted(n) != sorted(NODE_KEYS):
            e.append("%s keys invalid" % p)
            continue
        if not isinstance(n["id"], str) or not _ID_RE.match(n["id"]):
            e.append("%s.id must be 16 hex" % p)
        if n["id"] in ids:
            e.append("%s.id duplicated" % p)
        if n["parent"] is not None and (n["parent"] == n["id"] or n["parent"] not in ids):
            e.append("%s.parent must precede the node" % p)
        ids.add(n["id"])
        for k in ("depth", "ordinal"):
            if not _is_int(n[k]) or n[k] < 0:
                e.append("%s.%s must be a non-negative int" % (p, k))
        if not isinstance(n["role"], str) or not isinstance(n["name"], str):
            e.append("%s role/name must be strings" % p)
        if not isinstance(n["actionable"], bool):
            e.append("%s.actionable must be bool" % p)
        if not isinstance(n["states"], dict) or not all(isinstance(v, str) for v in n["states"].values()):
            e.append("%s.states must map to strings" % p)
        b = n["box"]
        if b is not None and not (isinstance(b, list) and len(b) == 4 and all(_is_int(v) for v in b)
                                  and b[2] >= 0 and b[3] >= 0):
            e.append("%s.box must be null or [x,y,w,h] ints with w,h >= 0" % p)
    if s["actionable"] != [n["id"] for n in s["nodes"] if isinstance(n, dict) and n.get("actionable")]:
        e.append("actionable must list actionable node ids in document order")
    c = s["counts"]
    if not isinstance(c, dict) or c.get("nodes") != len(s["nodes"]) or c.get("actionable") != len(s["actionable"]) \
            or not _is_int(c.get("raw")):
        e.append("counts inconsistent")
    try:
        _no_float(s)
    except StateError as x:
        e.append(str(x))
    return e


# @anchor:wa.state.schema.validate_target_descriptor
def validate_target_descriptor(envelope, *, expected_epoch=None, registry_epoch=None):
    """Return a list of violations (empty = valid) for a target descriptor envelope."""
    e = []
    if not isinstance(envelope, dict):
        return ["envelope is not an object"]

    try:
        _no_float(envelope)
    except StateError as x:
        e.append(str(x))

    if sorted(envelope.keys()) != sorted(TARGET_DESCRIPTOR_KEYS):
        e.append("envelope keys %s != %s" % (sorted(envelope.keys()), sorted(TARGET_DESCRIPTOR_KEYS)))

    if "epoch" in envelope:
        epoch = envelope["epoch"]
        if not _is_int(epoch) or epoch < 0:
            e.append("epoch must be a non-negative int")
        target_epoch = expected_epoch if expected_epoch is not None else registry_epoch
        if target_epoch is not None and _is_int(epoch) and epoch != target_epoch:
            e.append("epoch %d does not match registry epoch %d" % (epoch, target_epoch))

    if "frame_seq" in envelope:
        frame_seq = envelope["frame_seq"]
        if not _is_int(frame_seq) or frame_seq < 0:
            e.append("frame_seq must be a non-negative int")

    if "dpr_ppm" in envelope:
        dpr_ppm = envelope["dpr_ppm"]
        if not _is_int(dpr_ppm) or dpr_ppm <= 0:
            e.append("dpr_ppm must be a positive int")

    if "handle" in envelope:
        h = envelope["handle"]
        if isinstance(h, dict):
            if sorted(h.keys()) != sorted(HANDLE_KEYS):
                e.append("handle keys %s != %s" % (sorted(h.keys()), sorted(HANDLE_KEYS)))
            kind = h.get("kind")
            canonical_id = h.get("canonical_id")
            generation = h.get("generation")
        elif hasattr(h, "kind") and hasattr(h, "canonical_id") and hasattr(h, "generation"):
            if dataclasses.is_dataclass(h) and not isinstance(h, type):
                f_names = [f.name for f in dataclasses.fields(h)]
                if sorted(f_names) != sorted(HANDLE_KEYS):
                    e.append("handle fields %s != %s" % (sorted(f_names), sorted(HANDLE_KEYS)))
            kind = getattr(h, "kind", None)
            canonical_id = getattr(h, "canonical_id", None)
            generation = getattr(h, "generation", None)
        else:
            e.append("handle must be a Handle or dict")
            kind = canonical_id = generation = None

        if kind is not None or canonical_id is not None or generation is not None:
            if not isinstance(kind, str) or kind not in TARGET_KINDS:
                e.append("handle.kind must be one of %s" % (TARGET_KINDS,))
            if not isinstance(canonical_id, str) or len(canonical_id) == 0:
                e.append("handle.canonical_id must be a non-empty str")
            if not _is_int(generation) or generation < 0:
                e.append("handle.generation must be a non-negative int")

    if "state" in envelope:
        inner = envelope["state"]
        state_errs = validate_state(inner)
        e.extend(state_errs)
    else:
        e.append("state is required")

    seen = set()
    deduped = []
    for err in e:
        if err not in seen:
            seen.add(err)
            deduped.append(err)
    return deduped


# @anchor:wa.state.schema.build_target_descriptor
def build_target_descriptor(handle, epoch, state, *, frame_seq=0, dpr_ppm=1000000):
    """Assemble and validate a target descriptor envelope. Returns dict or raises StateError."""
    envelope = {
        "handle": handle,
        "epoch": epoch,
        "frame_seq": frame_seq,
        "dpr_ppm": dpr_ppm,
        "state": state,
    }
    errs = validate_target_descriptor(envelope)
    if errs:
        raise StateError("; ".join(errs))
    return envelope

# @anchor:wa.state.schema.canonical_actuation_resource
def canonical_actuation_resource(handle):
    """Return the canonical actuation resource identifier for a target handle."""
    if isinstance(handle, dict):
        return handle.get("canonical_id")
    return getattr(handle, "canonical_id", None)


# W1 companion data stays outside the closed wender.state/v1 contract.
import hashlib

COMPANION_SCHEMA_ID = "wender.tags.sidecar/v1"
COMPANION_TOP_KEYS = ("ax_sha256", "ax_stable_sha256", "elements", "gen16", "schema", "srf_state_hash")
PACKED_ELEMENT_KEYS = ("anchor_id", "box", "gen16", "id", "mark_id", "role", "state_word", "value_len")
STATE_WORD_VALID_MASK = 0x1FFF
STATE_WORD_MAX = 0xFFFF
BIT_DISABLED = 1 << 3
BIT_HIDDEN = 1 << 5
BIT_ACTIONABLE = 1 << 9
BIT_VISIBLE = 1 << 10
BIT_ENABLED = 1 << 11
M_ACT = BIT_ACTIONABLE | BIT_VISIBLE | BIT_ENABLED


def _packed_word_valid(word):
    return (type(word) is int and 0 <= word <= STATE_WORD_MAX
            and not word & ~STATE_WORD_VALID_MASK
            and not (word & BIT_DISABLED and word & BIT_ENABLED)
            and not (word & BIT_HIDDEN and word & BIT_VISIBLE))


def _packed_anchor_valid(anchor):
    return (isinstance(anchor, str) and 1 <= len(anchor) <= 128 and not anchor.isdecimal()
            and all(c.isprintable() and not c.isspace() for c in anchor))


# @anchor:wa.state.schema.PackedElement
@dataclasses.dataclass(frozen=True)
class PackedElement:
    anchor_id: str
    box: tuple | None
    gen16: int
    id: str
    mark_id: int | None
    role: str
    state_word: int
    value_len: int | None

    # @anchor:wa.state.schema.PackedElement.to_dict
    def to_dict(self):
        result = dataclasses.asdict(self)
        if self.box is not None:
            result["box"] = list(self.box)
        return result


@dataclasses.dataclass(frozen=True)
class _BottomElement:
    is_bottom: bool = True


BOTTOM_ELEMENT = _BottomElement()


# @anchor:wa.state.schema.srf_state_hash
def srf_state_hash(elements):
    rows = [(e.id, e.state_word, e.value_len) if isinstance(e, PackedElement)
            else (e["id"], e["state_word"], e["value_len"]) for e in elements]
    payload = sorted((i, w, -1 if v is None else v) for i, w, v in rows)
    return hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()[:16]


# @anchor:wa.state.schema.build_companion_projection
def build_companion_projection(nodes, state, *, gen16=None, mark_map=None):
    errors = validate_state(state)
    if errors:
        raise StateError("; ".join(errors))
    if [n.get("id") for n in nodes] != [n["id"] for n in state["nodes"]]:
        raise StateError("companion nodes must match STATE order and membership")
    generations = {n.get("gen16") for n in nodes}
    if gen16 is None:
        if len(generations) > 1:
            raise StateError("mixed generations")
        gen16 = next(iter(generations)) if generations else 0
    mark_map = {} if mark_map is None else mark_map
    if not isinstance(mark_map, dict) or set(mark_map) - {n["id"] for n in nodes}:
        raise StateError("unknown mark_map ids")
    elements = []
    for node, base in zip(nodes, state["nodes"]):
        if node.get("gen16") != gen16 or type(node.get("gen16")) is not int:
            raise StateError("node generation mismatch")
        if node.get("role") != base["role"]:
            raise StateError("node role mismatch")
        elements.append(PackedElement(node.get("anchor_id"), base["box"], gen16, node["id"],
                                      mark_map.get(node["id"]), base["role"], node.get("state_word"),
                                      node.get("value_len")).to_dict())
    sidecar = {"schema": COMPANION_SCHEMA_ID, "ax_sha256": state["ax_sha256"],
               "ax_stable_sha256": state["ax_stable_sha256"], "gen16": gen16,
               "elements": elements, "srf_state_hash": srf_state_hash(elements)}
    errors = validate_companion_projection(state, sidecar)
    if errors:
        raise StateError("; ".join(errors))
    return sidecar


# @anchor:wa.state.schema.validate_companion_projection
def validate_companion_projection(state, sidecar):
    errors = validate_state(state)
    if errors:
        return errors
    if not isinstance(sidecar, dict) or set(sidecar) != set(COMPANION_TOP_KEYS):
        return ["companion keys invalid"]
    if sidecar["schema"] != COMPANION_SCHEMA_ID:
        errors.append("companion schema invalid")
    gen = sidecar["gen16"]
    if type(gen) is not int or not 0 <= gen <= STATE_WORD_MAX:
        errors.append("companion gen16 invalid")
    for key in ("ax_sha256", "ax_stable_sha256"):
        if sidecar[key] != state[key]:
            errors.append("companion " + key + " mismatch")
    elements = sidecar["elements"]
    if not isinstance(elements, list):
        return errors + ["elements must be a list"]
    if len(elements) != len(state["nodes"]):
        errors.append("element count mismatch")
    marks = set()
    hashable = True
    for index, elem in enumerate(elements):
        prefix = "elements[%d]" % index
        if not isinstance(elem, dict) or set(elem) != set(PACKED_ELEMENT_KEYS):
            errors.append(prefix + " keys invalid")
            hashable = False
            continue
        base = state["nodes"][index] if index < len(state["nodes"]) else None
        if base is None or any(elem[k] != base[k] for k in ("id", "role", "box")):
            errors.append(prefix + " STATE binding mismatch")
        box = elem["box"]
        if box is not None and not (isinstance(box, list) and len(box) == 4
                                    and all(type(v) is int for v in box)
                                    and box[2] >= 0 and box[3] >= 0):
            errors.append(prefix + " box invalid")
        if type(elem["gen16"]) is not int or elem["gen16"] != gen:
            errors.append(prefix + " gen16 mismatch")
        if not _packed_anchor_valid(elem["anchor_id"]):
            errors.append(prefix + " anchor_id invalid")
        if not _packed_word_valid(elem["state_word"]):
            errors.append(prefix + " state_word invalid")
        value = elem["value_len"]
        if ((elem["role"] in ("textbox", "searchbox", "combobox")
             and (type(value) is not int or value < 0))
                or (elem["role"] not in ("textbox", "searchbox", "combobox") and value is not None)):
            errors.append(prefix + " value_len invalid")
        if type(elem["state_word"]) is int and bool(elem["state_word"] & (1 << 12)) != (type(value) is int and value > 0):
            errors.append(prefix + " value_nonempty mismatch")
        mark = elem["mark_id"]
        if mark is not None:
            if type(mark) is not int or not 1 <= mark <= 24 or mark in marks:
                errors.append(prefix + " mark_id invalid or duplicated")
            else:
                marks.add(mark)
    try:
        _no_float(sidecar)
        if hashable and sidecar["srf_state_hash"] != srf_state_hash(elements):
            errors.append("srf_state_hash mismatch")
    except (StateError, TypeError, ValueError):
        errors.append("invalid companion numeric or hash payload")
    return errors


# @anchor:wa.state.schema.merge_packed_elements
def merge_packed_elements(a, b):
    """Meet agreeing authority; optional evidence joins, conflicts absorb forever."""
    if a == BOTTOM_ELEMENT or b == BOTTOM_ELEMENT:
        return BOTTOM_ELEMENT
    if not isinstance(a, PackedElement) or not isinstance(b, PackedElement):
        return BOTTOM_ELEMENT
    if not _packed_word_valid(a.state_word) or not _packed_word_valid(b.state_word):
        return BOTTOM_ELEMENT
    for key in ("id", "anchor_id", "gen16", "role"):
        if getattr(a, key) != getattr(b, key):
            return BOTTOM_ELEMENT
    values = {}
    for key in ("box", "mark_id", "value_len"):
        x, y = getattr(a, key), getattr(b, key)
        if x is not None and y is not None and x != y:
            return BOTTOM_ELEMENT
        values[key] = y if x is None else x
    word = a.state_word | b.state_word
    if not _packed_word_valid(word):
        return BOTTOM_ELEMENT
    return dataclasses.replace(a, state_word=word, **values)


# @anchor:wa.state.schema.build_tags_sidecar
build_tags_sidecar = build_companion_projection
# @anchor:wa.state.schema.validate_tags_sidecar
validate_tags_sidecar = validate_companion_projection
# @anchor:wa.state.schema.merge_nodes
merge_nodes = merge_packed_elements
