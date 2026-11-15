# @module {"id":"wa.remember.program_map","layer":1,"owner":"REMEMBER","deps":["wa.state.schema"],"book":"HB-WA-s3-remember"}
"""Typed state identities and canonical, independently owned transition records.

The v1 key encoding does not migrate old journals. Store owns persistence and
must explicitly migrate legacy keys; this module never creates alias nodes.
Public nodes/edges remain the Store reconstruction interface. Method inputs
and returned records are copied; direct edits of those public containers are
deliberate graph edits, not an ownership-protected read API.
"""
from copy import deepcopy

from ..state.schema import dumps

_PIXEL_KEYS = frozenset(("frame", "frames", "pixels", "image", "screenshot", "png"))
_STATE_SCHEMA = "wender.state-key/v1"


def _text(value):
    if type(value) is not str:
        raise ValueError("metadata strings and object keys must be exact str")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("metadata must be valid UTF-8 text") from exc


def _metadata(value, active):
    kind = type(value)
    if value is None or kind in (bool, int):
        return
    if kind is str:
        _text(value)
        return
    if kind not in (list, dict):
        raise ValueError("unsupported metadata type: " + kind.__name__)
    identity = id(value)
    if identity in active:
        raise ValueError("cyclic metadata")
    active.add(identity)
    try:
        if kind is dict:
            for key, child in value.items():
                _text(key)
                if key.casefold() in _PIXEL_KEYS:
                    raise ValueError("reserved pixel key: " + key)
                _metadata(child, active)
        else:
            for child in value:
                _metadata(child, active)
    finally:
        active.remove(identity)


# @anchor:wa.remember.program_map.canonical_json
def canonical_json(value):
    """Canonical integer-only metadata, without type/key coercion or pixels.

    This structural guard cannot discover encoded images hidden in arbitrary
    strings. Producers must supply metadata, never encoded frame contents.
    """
    try:
        _metadata(value, set())
        return dumps(value)
    except (RecursionError, OverflowError) as exc:
        raise ValueError("metadata exceeds local serialization capacity") from exc


# @anchor:wa.remember.program_map.state_key
def state_key(screen_fp, ax_stable_sha):
    """Injective representation of exact None, bool, int and UTF-8 str pairs."""
    components = []
    for value in (screen_fp, ax_stable_sha):
        if value is None:
            tag = "none"
        elif type(value) is bool:
            tag = "bool"
        elif type(value) is int:
            tag = "int"
        elif type(value) is str:
            tag = "str"
        else:
            raise ValueError("state component must be exact None/bool/int/str")
        components.append([tag, value])
    return canonical_json([_STATE_SCHEMA, components[0], components[1]])


# @anchor:wa.remember.program_map.canonicalize_state_key
def canonicalize_state_key(stored_key, screen_fp, ax_stable_sha):
    """Validate a stored spelling against its raw identity and return a typed key.

    Legacy text is never split: sentinels and embedded colons have lost type or
    boundary information. The journal's raw fields supply that information.
    A reader must reject a legacy spelling reused for different raw identities
    and remap edge endpoints before discarding its old-to-new key table.
    Store integration must call this explicitly; state_key stays context-free.
    """
    canonical = state_key(screen_fp, ax_stable_sha)
    _text(stored_key)
    legacy = "%s:%s" % (screen_fp or "-", ax_stable_sha or "-")
    if stored_key != canonical and stored_key != legacy:
        raise ValueError("node key")
    return canonical


def _edge_order(edge):
    return (edge["pre"], canonical_json(edge["action"]), edge["post"],
            canonical_json(edge["changed"]))


# @anchor:wa.remember.program_map.ProgramMap
class ProgramMap:
    def __init__(self, program):
        _text(program)
        self.program = program
        self.nodes = {}   # key -> {"screen_fp", "ax_stable_sha", "seen"}
        self.edges = []   # {"pre", "post", "action", "changed"}

    def observe(self, screen_fp, ax_stable_sha):
        key = state_key(screen_fp, ax_stable_sha)
        node = self.nodes.setdefault(key, {
            "screen_fp": screen_fp, "ax_stable_sha": ax_stable_sha, "seen": 0})
        node["seen"] += 1
        return key

    def add_edge(self, pre, action, post, changed=None):
        # Preserve pre-before-post endpoint rejection and validate before mutation.
        for key in (pre, post):
            if key not in self.nodes:
                raise KeyError("unknown state %s" % key)
        edge = {"pre": pre, "action": action, "post": post, "changed": changed}
        identity = canonical_json(edge)
        for existing in self.edges:
            if canonical_json(existing) == identity:
                return deepcopy(existing)
        self.edges.append(deepcopy(edge))
        return deepcopy(edge)

    def successors(self, key):
        # Unknown keys retain the established empty-list query behavior.
        return deepcopy(sorted((edge for edge in self.edges if edge["pre"] == key),
                               key=_edge_order))

    def snapshot(self):
        return deepcopy({"schema": "wender.program_map/v0", "program": self.program,
                         "nodes": self.nodes, "edges": sorted(self.edges, key=_edge_order)})

    def to_json(self):
        return canonical_json(self.snapshot())
