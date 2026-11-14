# @module {"id":"wa.perceive.ax","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.cdp_pipe"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.ax - pruned accessibility tree with a reload-stable sha256 (prune_ax_v1).

ORIGIN: ported and adapted from the operator's own code,
OpenFresnel companions/browser/ax.py (lines 41-300 plus the canonical
form, fingerprints and matching ladder that follow). The original is Spanish and Playwright-bound;
this port is English, transport-agnostic (any object with `send(method, params, session_id)`,
e.g. wender.perceive.cdp_pipe.CdpPipe) and retains the reference prune rules except
for the hardened serialized-name byte bound below:

 1. Depth-first walk from the root(s) in `childIds` order (document order, not CDP list order).
 2. A node is EMITTED iff `ignored == false` and its role is in CHASSIS_ROLES; others
    (generic, none, StaticText, InlineTextBox, ...) are traversed without emitting.
 3. Never descend into OPAQUE_ROLES (article): emitted, interior skipped (provider content).
 4. `depth` counts emitted ancestors only.
 5. One canonical line per emitted node: `<depth>|<role>|<name>|<attrs>`.
 6. Name: NFC -> collapse whitespace -> strip -> escape `\\` and `|` -> cut to
    64 serialized UTF-8 bytes, keeping complete code points and escape tokens.
 7. attrs = sorted `k=v` of STATE_ATTRS joined by `,`. focusable/focused/invalid are excluded
    (they flip by themselves between two reads of the same page, measured in the origin).
 8. Blob = "\\n".join(lines) + "\\n"; sha256 of its UTF-8 bytes. The STABLE variant has attrs
    always empty.
Wender addition: each emitted node carries `backend_id` (CDP backendDOMNodeId, not hashed) so
boxes can be resolved, and a stable `id` derived from hashed fields only.
"""
import difflib
import hashlib
import re
import time
import unicodedata

PRUNE_NAME = "prune_ax_v1"
PRUNE_VERSION = 1
MAX_NAME_BYTES = 64  # inherited estimate from the origin, not a measured optimum

CHASSIS_ROLES = frozenset((
    "RootWebArea", "main", "navigation", "banner", "contentinfo", "complementary",
    "region", "search", "form", "dialog", "alertdialog", "toolbar",
    "menu", "menubar", "menuitem", "menuitemcheckbox", "menuitemradio",
    "tablist", "tab", "tabpanel", "heading",
    "button", "link", "textbox", "searchbox", "combobox", "listbox", "option",
    "checkbox", "radio", "switch", "slider", "spinbutton",
    "status", "alert", "log", "progressbar", "timer", "marquee",
    "article", "note", "group", "application",
))
OPAQUE_ROLES = frozenset(("article",))
STATE_ATTRS = ("busy", "checked", "current", "disabled",
               "expanded", "hidden", "pressed", "selected")
ACTIONABLE_ROLES = frozenset((
    "button", "link", "textbox", "searchbox", "combobox", "listbox", "option",
    "checkbox", "radio", "switch", "slider", "spinbutton", "menuitem",
    "menuitemcheckbox", "menuitemradio", "tab",
))
MATCH_LEVELS = ("L0_EXACT", "L1_STABLE", "L2_NAME", "L3_VARIANT", "L4_UNIQUE_ROLE")
AX_RETRIES = 3
AX_RETRY_WAIT_S = 0.25

VALUE_ROLES = frozenset(("textbox", "searchbox", "combobox"))
SHIFT_BUSY = 0
BIT_BUSY = 1 << SHIFT_BUSY
SHIFT_CHECKED = 1
BIT_CHECKED = 1 << SHIFT_CHECKED
SHIFT_CURRENT = 2
BIT_CURRENT = 1 << SHIFT_CURRENT
SHIFT_DISABLED = 3
BIT_DISABLED = 1 << SHIFT_DISABLED
SHIFT_EXPANDED = 4
BIT_EXPANDED = 1 << SHIFT_EXPANDED
SHIFT_HIDDEN = 5
BIT_HIDDEN = 1 << SHIFT_HIDDEN
SHIFT_PRESSED = 6
BIT_PRESSED = 1 << SHIFT_PRESSED
SHIFT_SELECTED = 7
BIT_SELECTED = 1 << SHIFT_SELECTED
SHIFT_FOCUSED = 8
BIT_FOCUSED = 1 << SHIFT_FOCUSED
SHIFT_ACTIONABLE = 9
BIT_ACTIONABLE = 1 << SHIFT_ACTIONABLE
SHIFT_VISIBLE = 10
BIT_VISIBLE = 1 << SHIFT_VISIBLE
SHIFT_ENABLED = 11
BIT_ENABLED = 1 << SHIFT_ENABLED
SHIFT_VALUE_NONEMPTY = 12
BIT_VALUE_NONEMPTY = 1 << SHIFT_VALUE_NONEMPTY
STATE_WORD_VALID_MASK = 0x1FFF
STATE_WORD_RESERVED_MASK = 0xE000
STATE_WORD_MAX = 0xFFFF
M_ACT = BIT_ACTIONABLE | BIT_VISIBLE | BIT_ENABLED
_STATE_FLAGS = ("busy", "checked", "current", "disabled", "expanded", "hidden",
                "pressed", "selected", "focused", "actionable", "visible", "enabled", "value_nonempty")


# @anchor:wa.perceive.ax.normalize_name
def normalize_name(raw):
    """NFC -> whitespace collapse -> strip -> escape -> bounded serialized UTF-8."""
    if not raw:
        return ""
    t = unicodedata.normalize("NFC", str(raw))
    t = re.sub(r"\s+", " ", t).strip()
    t = t.replace("\\", "\\\\").replace("|", "\\|")
    b = t.encode("utf-8")
    if len(b) > MAX_NAME_BYTES:
        t = b[:MAX_NAME_BYTES].decode("utf-8", "ignore")
        # Only an odd terminal run leaves half an escape token. Preserve pairs.
        if (len(t) - len(t.rstrip("\\"))) % 2:
            t = t[:-1]
    return t


# @anchor:wa.perceive.ax.sha256_text
def sha256_text(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


# @anchor:wa.perceive.ax.raw_tree
def raw_tree(cdp, session_id):
    """Flat AX node list from Accessibility.getFullAXTree, with bounded retries.

    A transient CDP failure is not a page change; reading does not touch the page, so a
    blind retry is safe. After AX_RETRIES the error propagates (callers fail closed).
    """
    last = None
    for attempt in range(AX_RETRIES):
        try:
            cdp.send("Accessibility.enable", session_id=session_id)
            return cdp.send("Accessibility.getFullAXTree", session_id=session_id).get("nodes") or []
        except Exception as e:  # transport-agnostic: any failure is retried
            last = e
            if attempt + 1 < AX_RETRIES:
                time.sleep(AX_RETRY_WAIT_S)
    raise RuntimeError("AX tree unreadable after %d attempts: %s" % (AX_RETRIES, last))


# @anchor:wa.perceive.ax.release
def release(cdp, session_id):
    """Accessibility.disable: Chromium keeps the full AX tree live while enabled."""
    try:
        cdp.send("Accessibility.disable", session_id=session_id)
        return True
    except Exception:
        return False


def _value(v):
    if not isinstance(v, dict):
        return ""
    x = v.get("value")
    if isinstance(x, bool):
        return "true" if x else "false"
    if x is None:
        return ""
    return str(x)


def _props(node):
    return {p.get("name"): _value(p.get("value")) for p in (node.get("properties") or [])
            if p.get("name") not in ("value", "password")}


# @anchor:wa.perceive.ax.utf16_len
def utf16_len(text):
    return len(str(text).encode("utf-16-le", errors="surrogatepass")) // 2


# @anchor:wa.perceive.ax.validate_state_word
def validate_state_word(word):
    if (type(word) is not int or not 0 <= word <= STATE_WORD_MAX
            or word & ~STATE_WORD_VALID_MASK
            or (word & BIT_DISABLED and word & BIT_ENABLED)
            or (word & BIT_HIDDEN and word & BIT_VISIBLE)):
        raise ValueError("invalid state_word")
    return word


# @anchor:wa.perceive.ax.encode_state_word
def encode_state_word(props, role="", value_len=None):
    props = dict(props)
    flags = [props.get(k) in (("true", "mixed") if k in ("checked", "pressed") else ("true",))
             for k in _STATE_FLAGS[:9]]
    flags += [role in ACTIONABLE_ROLES, props.get("hidden") != "true",
              props.get("disabled") != "true", type(value_len) is int and value_len > 0]
    return validate_state_word(sum(1 << i for i, flag in enumerate(flags) if flag))


# @anchor:wa.perceive.ax.decode_state_word
def decode_state_word(word):
    validate_state_word(word)
    return {k: bool(word & (1 << i)) for i, k in enumerate(_STATE_FLAGS)}


# @anchor:wa.perceive.ax.is_ready_to_act
def is_ready_to_act(state_word):
    word = validate_state_word(state_word)
    return word & M_ACT == M_ACT and not word & (BIT_DISABLED | BIT_HIDDEN)


def _uint16(value):
    if type(value) is not int or not 0 <= value <= STATE_WORD_MAX:
        raise ValueError("gen16 must be uint16")
    return value


# @anchor:wa.perceive.ax.gen16_newer
def gen16_newer(a, b):
    return 1 <= ((_uint16(a) - _uint16(b)) & STATE_WORD_MAX) <= 32767


# @anchor:wa.perceive.ax.gen16_fresh
def gen16_fresh(node_gen16, current_epoch_gen16):
    return _uint16(node_gen16) == _uint16(current_epoch_gen16)


def _structure(nodes):
    return [(tuple(n["ancestors"]), n["role"], n["name"], n["ordinal"], n["depth"])
            for n in nodes]


# @anchor:wa.perceive.ax.next_gen16
def next_gen16_on_structural_change(prev_nodes, curr_nodes, current_gen16=0):
    base = _uint16(current_gen16)
    generations = {_uint16(n["gen16"]) for n in prev_nodes}
    if len(generations) > 1:
        raise ValueError("mixed previous generations")
    if base == 0 and generations:
        base = next(iter(generations))
    return (base + (_structure(prev_nodes) != _structure(curr_nodes))) & STATE_WORD_MAX


# @anchor:wa.perceive.ax.verify_gen16_transition
def verify_gen16_transition(prev_nodes, curr_nodes):
    expected = next_gen16_on_structural_change(prev_nodes, curr_nodes)
    if any(_uint16(n["gen16"]) != expected for n in curr_nodes):
        raise ValueError("stale structural generation")
    return True


# @anchor:wa.perceive.ax.validate_anchor_id
def validate_anchor_id(anchor_id):
    if (not isinstance(anchor_id, str) or not 1 <= len(anchor_id) <= 128
            or anchor_id.isdecimal() or not all(c.isprintable() and not c.isspace() for c in anchor_id)):
        raise ValueError("invalid anchor_id")
    return anchor_id


def _extract_anchor_id(props, stable_node_id):
    for key in ("data-agent-id", "anchor_id", "data-testid"):
        candidate = unicodedata.normalize("NFC", props.get(key, "").strip())[:128]
        try:
            return validate_anchor_id(candidate)
        except ValueError:
            pass
    return "ax:" + stable_node_id


# @anchor:wa.perceive.ax.validate_anchor_stability
def validate_anchor_stability(nodes_a, nodes_b):
    a = {n["id"]: validate_anchor_id(n.get("anchor_id")) for n in nodes_a}
    b = {n["id"]: validate_anchor_id(n.get("anchor_id")) for n in nodes_b}
    if a != b:
        raise ValueError("unstable anchors")
    return True


def _value_len(node, role):
    if role not in VALUE_ROLES:
        return None
    raw = node.get("value")
    if "value" not in node:
        raw = next((p.get("value") for p in reversed(node.get("properties") or [])
                    if p.get("name") == "value"), None)
    return utf16_len(raw if isinstance(raw, str) else _value(raw))


# @anchor:wa.perceive.ax.prune
def prune(raw_nodes, *, gen16=0, prev_nodes=None):
    """Apply prune_ax_v1. Return emitted nodes in document order."""
    _uint16(gen16)
    by_id = {n.get("nodeId"): n for n in raw_nodes if n.get("nodeId") is not None}
    children = {c for n in raw_nodes for c in (n.get("childIds") or [])}
    roots = [n for n in raw_nodes if n.get("nodeId") not in children] or raw_nodes[:1]
    out = []
    seen = set()  # cycles should not exist, but this is external data

    def walk(node, depth, ancestors):
        stack = [(node, depth, ancestors)]
        # explicit stack, children pushed reversed -> same pre-order as recursion
        while stack:
            nd, dp, anc = stack.pop()
            nid = nd.get("nodeId")
            if nid in seen:
                continue
            seen.add(nid)
            role = _value(nd.get("role"))
            if (not nd.get("ignored")) and role in CHASSIS_ROLES:
                props = _props(nd)
                value_len = _value_len(nd, role)
                out.append({
                    "value_len": value_len,
                    "state_word": encode_state_word(props, role, value_len),
                    "role": role,
                    "name": normalize_name(_value(nd.get("name"))),
                    "attrs": tuple((k, props[k]) for k in STATE_ATTRS if k in props),
                    "depth": dp,
                    "ancestors": tuple(anc),
                    "node_id": nid,
                    "backend_id": nd.get("backendDOMNodeId"),
                })
                if role in OPAQUE_ROLES:
                    continue
                dp += 1
                anc = anc + (role,)
            kids = [by_id[c] for c in (nd.get("childIds") or []) if c in by_id]
            for k in reversed(kids):
                stack.append((k, dp, anc))

    for r in roots:
        walk(r, 0, ())
    count = {}
    for nd in out:
        k = (nd["role"], nd["name"])
        nd["ordinal"] = count.get(k, 0)
        count[k] = nd["ordinal"] + 1
    for nd in out:
        nd["cardinality"] = count[(nd["role"], nd["name"])]
        nd["id"] = stable_id(nd)
    effective = (next_gen16_on_structural_change(prev_nodes, out, gen16)
                 if prev_nodes is not None else gen16)
    for nd in out:
        nd["gen16"] = effective
        nd["anchor_id"] = _extract_anchor_id(_props(by_id[nd["node_id"]]), nd["id"])
    return out


def _attrs_txt(attrs):
    return ",".join("%s=%s" % (k, v) for k, v in attrs)


# @anchor:wa.perceive.ax.canonical_line
def canonical_line(node, with_state=True):
    return "%d|%s|%s|%s" % (node["depth"], node["role"], node["name"],
                            _attrs_txt(node["attrs"]) if with_state else "")


# @anchor:wa.perceive.ax.canon
def canon(nodes, with_state=True):
    if not nodes:
        return ""
    return "\n".join(canonical_line(n, with_state) for n in nodes) + "\n"


# @anchor:wa.perceive.ax.snapshot_sha
def snapshot_sha(nodes, with_state=True):
    return sha256_text(canon(nodes, with_state))


# @anchor:wa.perceive.ax.fingerprints
def fingerprints(node):
    """(exact, stable) per-node fingerprints, hex[16], as in the origin (section 11.2)."""
    base = "/".join(node["ancestors"]) + "\0" + node["role"] + "\0" + node["name"]
    exact = hashlib.sha256((base + "\0" + _attrs_txt(node["attrs"])).encode("utf-8")).hexdigest()[:16]
    stable = hashlib.sha256((base + "\0").encode("utf-8")).hexdigest()[:16]
    return exact, stable


# @anchor:wa.perceive.ax.stable_id
def stable_id(node):
    """Wender element id: stable fingerprint + ordinal. Reload-stable; independent of
    CDP nodeId/backendDOMNodeId and of state attrs."""
    return hashlib.sha256(("%s\0%d" % (fingerprints(node)[1], node["ordinal"])).encode("utf-8")).hexdigest()[:16]


# @anchor:wa.perceive.ax.context_fingerprint
def context_fingerprint(nodes, url_normalized, title):
    return {
        "url_normalized": url_normalized,
        "title_sha256": sha256_text(title or ""),
        "n_actionable": sum(1 for n in nodes if n["role"] in ACTIONABLE_ROLES),
        "roles_present": sorted({n["role"] for n in nodes}),
        "chassis_sha256": snapshot_sha(nodes, with_state=False),
        "prune_version": PRUNE_VERSION,
    }


# @anchor:wa.perceive.ax.context_matches
def context_matches(recorded, now):
    """Same page? Decided by coarse signals; the chassis sha is kept but does not vote."""
    broken = []
    for f in ("url_normalized", "title_sha256", "n_actionable"):
        if recorded.get(f) != now.get(f):
            broken.append({"field": f, "before": recorded.get(f), "now": now.get(f)})
    if set(recorded.get("roles_present") or []) != set(now.get("roles_present") or []):
        broken.append({"field": "roles_present", "before": recorded.get("roles_present"),
                       "now": now.get("roles_present")})
    return (not broken), broken


def _score(entry, node):
    """Tie-break INSIDE one level only; never crosses levels. Diagnostic, not a gate."""
    a = "%s|%s|%s" % (entry.get("role"), entry.get("name"), entry.get("fp_stable"))
    b = "%s|%s|%s" % (node["role"], node["name"], fingerprints(node)[1])
    r = difflib.SequenceMatcher(None, a, b).ratio()
    d = abs(int(entry.get("ordinal") or 0) - node["ordinal"])
    return round(max(0.0, r - min(0.05, 0.01 * d)), 6)


# @anchor:wa.perceive.ax.describe
def describe(node):
    ex, st = fingerprints(node)
    return {"role": node["role"], "name": node["name"], "ordinal": node["ordinal"],
            "cardinality": node["cardinality"], "fp_exact": ex, "fp_stable": st}


# @anchor:wa.perceive.ax.match
def match(nodes, entry):
    """Re-resolve a recorded element (from `describe`) against the current snapshot.

    Ladder L0_EXACT -> L1_STABLE -> L2_NAME -> L3_VARIANT -> L4_UNIQUE_ROLE.
    Returns {level|None, node, score, tie, candidates, ladder_tried}.
    """
    role, name = entry.get("role"), entry.get("name")
    ordinal = int(entry.get("ordinal") or 0)
    variants = list(entry.get("name_variants") or [])
    ladder = []

    def close(level, cands):
        if not cands:
            return None
        scored = sorted(((_score(entry, n), n) for n in cands), key=lambda t: (-t[0], t[1]["ordinal"]))
        tie = len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 0.05
        return {"level": level, "node": scored[0][1], "score": scored[0][0], "tie": tie,
                "candidates": len(cands), "ladder_tried": ladder}

    same = [n for n in nodes if n["role"] == role and n["name"] == name]
    at_ord = [n for n in same if n["ordinal"] == ordinal]
    ladder.append({"level": "L0_EXACT", "candidates": len(at_ord)})
    if entry.get("fp_exact") is None:
        r = close("L0_EXACT", at_ord)
        if r:
            r["established"] = True
            return r
    exact = [n for n in at_ord if fingerprints(n)[0] == entry.get("fp_exact")]
    if exact:
        return close("L0_EXACT", exact)
    ladder.append({"level": "L1_STABLE", "candidates": len(at_ord)})
    stable = [n for n in at_ord if fingerprints(n)[1] == entry.get("fp_stable")]
    if stable:
        return close("L1_STABLE", stable)
    others = [n for n in same if n["ordinal"] != ordinal]
    name_cands = others or at_ord
    ladder.append({"level": "L2_NAME", "candidates": len(name_cands)})
    if name_cands:
        return close("L2_NAME", name_cands)
    by_var = [n for n in nodes if n["role"] == role and n["name"] in variants]
    ladder.append({"level": "L3_VARIANT", "candidates": len(by_var)})
    if by_var:
        return close("L3_VARIANT", by_var)
    by_role = [n for n in nodes if n["role"] == role]
    ladder.append({"level": "L4_UNIQUE_ROLE", "candidates": len(by_role)})
    if len(by_role) == 1:
        return close("L4_UNIQUE_ROLE", by_role)
    return {"level": None, "node": None, "score": None, "tie": False, "candidates": 0,
            "ladder_tried": ladder}


# @anchor:wa.perceive.ax.level_allows_irreversible
def level_allows_irreversible(level):
    """Only L0/L1 may authorise an irreversible action (origin section 12.1)."""
    return level in ("L0_EXACT", "L1_STABLE")
