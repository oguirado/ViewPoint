# @module {"id":"wa.learn.property_fuzzer","layer":2,"owner":"LEARN","deps":["wa.state.schema","wa.act.coordinates"],"book":"HB-WA-s4-learn"}
"""@module wa.learn.property_fuzzer - zero-dependency SplitMix64 seed tree and bounded hierarchical shrinker.

Provides deterministic domain labels, stable child seeds, stdlib seeded random generation,
and validity-preserving hierarchical shrinking across UI trees, bounding boxes, DPR scales,
and coordinate action sequences.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import random

from wender.act import coordinates
from wender.state import schema

MASK64 = 0xFFFFFFFFFFFFFFFF
GOLDEN_GAMMA = 0x9E3779B97F4A7C15
MIX1 = 0xBF58476D1CE4E5B9
MIX2 = 0x94D049BB133111EB

DOMAINS = ("ui_tree", "bbox", "dpr_scale", "action_sequence")
DEFAULT_CASES_PER_DOMAIN = 64
MAX_SHRINK_ATTEMPTS = 32

_SAMPLE_ROLES = (
    "RootWebArea",
    "main",
    "navigation",
    "region",
    "form",
    "dialog",
    "group",
    "button",
    "link",
    "textbox",
    "searchbox",
    "combobox",
    "checkbox",
    "radio",
    "switch",
    "tab",
)
_ACTIONABLE_ROLES = frozenset((
    "button",
    "link",
    "textbox",
    "searchbox",
    "combobox",
    "checkbox",
    "radio",
    "switch",
    "tab",
))
_STATE_ATTR_KEYS = ("busy", "checked", "current", "disabled", "expanded", "hidden", "pressed", "selected")
_DPR_PRESETS = (500000, 750000, 1000000, 1250000, 1500000, 1750000, 2000000, 2500000, 3000000)


class FuzzerError(ValueError):
    """Raised when fuzzer parameters, seeds, or domain budgets are invalid."""


class FuzzerBudgetExhausted(RuntimeError):
    """Raised when a seeded campaign cannot generate the required valid cases within budget."""


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# @anchor:wa.learn.property_fuzzer.splitmix64
def mix64(z):
    """Pure 64-bit SplitMix64 finalizer mix."""
    if not _is_int(z):
        raise FuzzerError("seed value must be an int, not bool or float")
    z = z & MASK64
    z = ((z ^ (z >> 30)) * MIX1) & MASK64
    z = ((z ^ (z >> 27)) * MIX2) & MASK64
    return (z ^ (z >> 31)) & MASK64


def splitmix64_next(state):
    """Advance SplitMix64 state by GOLDEN_GAMMA and return (next_state, output_u64)."""
    if not _is_int(state):
        raise FuzzerError("splitmix64 state must be an int")
    next_state = (state + GOLDEN_GAMMA) & MASK64
    return next_state, mix64(next_state)


def _label_words(label):
    if not isinstance(label, str) or not label.strip():
        raise FuzzerError("domain or child label must be a non-empty string")
    raw = label.encode("utf-8")
    words = [len(raw) & MASK64]
    for offset in range(0, len(raw), 8):
        chunk = raw[offset:offset + 8].ljust(8, b"\x00")
        words.append(int.from_bytes(chunk, "little"))
    return words


# @anchor:wa.learn.property_fuzzer.SeedTree
@dataclass(frozen=True)
class SeedTree:
    """Zero-dependency SplitMix64 seed tree with deterministic domain labels and stable child seeds."""
    seed: int
    label: str = "root"
    path: tuple = ("root",)

    def __init__(self, seed, label="root", path=None):
        if not _is_int(seed) or seed < 0:
            raise FuzzerError("seed must be a non-negative int")
        if not isinstance(label, str) or not label.strip():
            raise FuzzerError("label must be a non-empty string")
        normalized_seed = seed & MASK64
        resolved_path = tuple(path) if path is not None else (label,)
        if not resolved_path or not all(isinstance(p, str) and p for p in resolved_path):
            raise FuzzerError("path must be a non-empty tuple of non-empty strings")
        object.__setattr__(self, "seed", normalized_seed)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "path", resolved_path)

    def child(self, label, index=0):
        """Derive a stable child SeedTree for (label, index) independent of sibling call order."""
        if not _is_int(index) or index < 0:
            raise FuzzerError("child index must be a non-negative int")
        acc = mix64(self.seed ^ GOLDEN_GAMMA)
        for w in _label_words(label):
            acc = mix64(acc ^ w)
        child_seed = mix64(acc ^ ((index + 1) * MIX1))
        child_label = f"{label}[{index}]" if index else label
        return SeedTree(child_seed, label=label, path=self.path + (child_label,))

    def domain(self, domain_label):
        """Derive the canonical domain child node for a registered domain label."""
        if domain_label not in DOMAINS:
            raise FuzzerError(f"unknown domain label: {domain_label!r}; expected one of {DOMAINS!r}")
        return self.child(domain_label, 0)

    def case(self, domain_label, case_index):
        """Derive the stable child seed for a specific (domain_label, case_index)."""
        return self.domain(domain_label).child("case", case_index)

    def rng(self):
        """Return a deterministic stdlib random.Random seeded from this node's 64-bit seed."""
        return random.Random(self.seed)

    def stream_u64(self, count):
        """Generate `count` deterministic 64-bit integers using SplitMix64."""
        if not _is_int(count) or count < 0:
            raise FuzzerError("count must be a non-negative int")
        state = self.seed
        out = []
        for _ in range(count):
            state, val = splitmix64_next(state)
            out.append(val)
        return out


@dataclass(frozen=True)
class CounterexampleMetadata:
    """Reproducible counterexample metadata produced by bounded hierarchical shrinking."""
    domain: str
    root_seed: int
    domain_seed: int
    case_seed: int
    case_index: int
    validity_class: str
    predicate_name: str
    original_fixture: object
    minimal_fixture: object
    shrink_attempts: int
    shrink_steps: int
    reproducible: bool
    path: tuple


# @anchor:wa.learn.property_fuzzer.validators
def is_valid_bbox(box):
    """Return True iff `box` is a valid positive [x, y, w, h] integer bounding box."""
    if not isinstance(box, list) or len(box) != 4:
        return False
    if not all(_is_int(v) for v in box):
        return False
    try:
        cx, cy = coordinates.box_center(box)
        return _is_int(cx) and _is_int(cy) and box[2] > 0 and box[3] > 0
    except coordinates.CoordRefused:
        return False


def is_valid_dpr_scale(case):
    """Return True iff `case` is a valid positive integer DPR scale or DPR conversion dict."""
    if _is_int(case):
        if case <= 0:
            return False
        try:
            coordinates.css_to_device(0, 0, case)
            return True
        except coordinates.CoordRefused:
            return False
    if isinstance(case, dict):
        if sorted(case.keys()) != ["dpr_ppm", "x", "y"]:
            return False
        x, y, dpr_ppm = case["x"], case["y"], case["dpr_ppm"]
        if not (_is_int(x) and _is_int(y) and _is_int(dpr_ppm) and dpr_ppm > 0):
            return False
        try:
            dx, dy = coordinates.css_to_device(x, y, dpr_ppm)
            return _is_int(dx) and _is_int(dy)
        except coordinates.CoordRefused:
            return False
    return False


def is_valid_ui_tree(state):
    """Return True iff `state` is a valid `wender.state/v1` dict with zero schema violations."""
    if not isinstance(state, dict):
        return False
    if schema.validate_state(state) != []:
        return False
    nodes = state.get("nodes", [])
    if not nodes:
        return False
    id_to_depth = {}
    for n in nodes:
        parent = n["parent"]
        if parent is None:
            if n["depth"] != 0:
                return False
        else:
            if parent not in id_to_depth:
                return False
            if n["depth"] != id_to_depth[parent] + 1:
                return False
        id_to_depth[n["id"]] = n["depth"]
    return True


def is_valid_action_sequence(fixture):
    """Return True iff `fixture` has a valid UI tree and non-empty plannable coordinate actions."""
    if not isinstance(fixture, dict) or sorted(fixture.keys()) != ["actions", "state"]:
        return False
    state = fixture["state"]
    actions = fixture["actions"]
    if not is_valid_ui_tree(state):
        return False
    if not isinstance(actions, list) or not actions:
        return False
    for act in actions:
        if not isinstance(act, dict):
            return False
        op = act.get("op")
        if op not in ("click", "hover", "lpath_click", "type"):
            return False
        try:
            if op == "click":
                events = coordinates.plan_click(
                    state, act.get("target"), irreversible=act.get("irreversible", False)
                )
            elif op == "hover":
                events = coordinates.plan_hover(
                    state, act.get("target"), irreversible=act.get("irreversible", False)
                )
            elif op == "lpath_click":
                events = coordinates.plan_lpath_click(
                    state,
                    act.get("target"),
                    act.get("origin"),
                    irreversible=act.get("irreversible", False),
                    waypoint_policy=act.get("waypoint_policy", "horizontal_first"),
                )
            else:
                events = coordinates.plan_type(act.get("text"))
            if not events:
                return False
        except coordinates.CoordRefused:
            return False
    return True


DOMAIN_VALIDATORS = {
    "ui_tree": is_valid_ui_tree,
    "bbox": is_valid_bbox,
    "dpr_scale": is_valid_dpr_scale,
    "action_sequence": is_valid_action_sequence,
}


def _compute_node_id(ancestors, role, name, ordinal):
    base = "/".join(ancestors) + "\0" + role + "\0" + name + "\0"
    stable_fp = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]
    return hashlib.sha256(f"{stable_fp}\0{ordinal}".encode("utf-8")).hexdigest()[:16]


def _compute_ax_hashes(nodes):
    if not nodes:
        empty_sha = hashlib.sha256(b"").hexdigest()
        return empty_sha, empty_sha
    lines_with_state = []
    lines_stable = []
    for n in nodes:
        states = n.get("states") or {}
        attrs_txt = ",".join(f"{k}={states[k]}" for k in sorted(states))
        lines_with_state.append(f"{n['depth']}|{n['role']}|{n['name']}|{attrs_txt}")
        lines_stable.append(f"{n['depth']}|{n['role']}|{n['name']}|")
    blob_state = ("\n".join(lines_with_state) + "\n").encode("utf-8")
    blob_stable = ("\n".join(lines_stable) + "\n").encode("utf-8")
    return hashlib.sha256(blob_state).hexdigest(), hashlib.sha256(blob_stable).hexdigest()


def rebuild_valid_state_metadata(state):
    """Recompute ordinals, actionable list, counts, and AX hashes after structural edits."""
    out = deepcopy(state)
    nodes = out["nodes"]
    out["actionable"] = [n["id"] for n in nodes if n.get("actionable")]
    raw_floor = len(nodes)
    prev_raw = out.get("counts", {}).get("raw", raw_floor)
    out["counts"] = {
        "raw": max(raw_floor, prev_raw if _is_int(prev_raw) else raw_floor),
        "nodes": len(nodes),
        "actionable": len(out["actionable"]),
    }
    ax_sha, ax_stable = _compute_ax_hashes(nodes)
    out["ax_sha256"] = ax_sha
    out["ax_stable_sha256"] = ax_stable
    return out


# @anchor:wa.learn.property_fuzzer.generators
def generate_bbox(seed_node):
    """Generate a valid [x, y, w, h] integer bounding box from a SeedTree node."""
    rng = seed_node.rng()
    x = rng.randint(-50, 500)
    y = rng.randint(-50, 400)
    w = rng.randint(1, 240)
    h = rng.randint(1, 180)
    return [x, y, w, h]


def generate_dpr_scale(seed_node):
    """Generate a valid DPR scale test case from a SeedTree node."""
    rng = seed_node.rng()
    if rng.randint(0, 1) == 0:
        dpr_ppm = rng.choice(_DPR_PRESETS)
    else:
        dpr_ppm = rng.randint(100000, 4000000)
    return {
        "x": rng.randint(0, 1920),
        "y": rng.randint(0, 1080),
        "dpr_ppm": dpr_ppm,
    }


def generate_ui_tree(seed_node, *, min_nodes=2, max_nodes=10, ensure_actionable=True):
    """Generate a valid `wender.state/v1` UI tree from a SeedTree node."""
    rng = seed_node.rng()
    kind = rng.choice(schema.SOURCE_KINDS)
    vw = rng.randint(320, 1600)
    vh = rng.randint(240, 1200)
    sx = rng.randint(0, 200)
    sy = rng.randint(0, 200)
    viewport = {"w": vw, "h": vh, "scroll_x": sx, "scroll_y": sy}

    num_nodes = rng.randint(max(1, min_nodes), max(min_nodes, max_nodes))
    raw_specs = []
    open_ancestors = []

    for idx in range(num_nodes):
        if idx == 0:
            role = "RootWebArea"
            depth = 0
            parent_idx = None
            anc_roles = ()
        else:
            choice_idx = rng.randint(0, len(open_ancestors) - 1)
            open_ancestors = open_ancestors[:choice_idx + 1]
            parent_idx, parent_depth, parent_anc = open_ancestors[-1]
            depth = parent_depth + 1
            anc_roles = parent_anc
            if ensure_actionable and idx == 1:
                role = rng.choice(tuple(_ACTIONABLE_ROLES))
            else:
                role = rng.choice(_SAMPLE_ROLES[1:])

        name = f"{role}_{idx}_{rng.randint(1, 99)}"
        states = {}
        if rng.randint(0, 2) == 0:
            attr_key = rng.choice(_STATE_ATTR_KEYS)
            if ensure_actionable and idx == 1 and attr_key == "disabled":
                states[attr_key] = "false"
            else:
                states[attr_key] = rng.choice(("true", "false"))

        bw = rng.randint(4, min(120, max(8, vw // 2)))
        bh = rng.randint(4, min(80, max(8, vh // 2)))
        bx = rng.randint(sx, max(sx, sx + vw - bw - 1))
        by = rng.randint(sy, max(sy, sy + vh - bh - 1))
        box = [bx, by, bw, bh]

        raw_specs.append({
            "role": role,
            "name": name,
            "depth": depth,
            "parent_idx": parent_idx,
            "ancestors": anc_roles,
            "states": states,
            "box": box,
        })
        open_ancestors.append((idx, depth, anc_roles + (role,)))

    role_name_counts = {}
    out_nodes = []
    for idx, spec in enumerate(raw_specs):
        rn_key = (spec["role"], spec["name"])
        ordinal = role_name_counts.get(rn_key, 0)
        role_name_counts[rn_key] = ordinal + 1
        node_id = _compute_node_id(spec["ancestors"], spec["role"], spec["name"], ordinal)
        while any(existing["id"] == node_id for existing in out_nodes):
            ordinal += 1
            role_name_counts[rn_key] = ordinal + 1
            node_id = _compute_node_id(spec["ancestors"], spec["role"], spec["name"], ordinal)
        parent_id = out_nodes[spec["parent_idx"]]["id"] if spec["parent_idx"] is not None else None
        is_act = (spec["role"] in _ACTIONABLE_ROLES) and (spec["states"].get("disabled") != "true")
        out_nodes.append({
            "id": node_id,
            "role": spec["role"],
            "name": spec["name"],
            "depth": spec["depth"],
            "parent": parent_id,
            "ordinal": ordinal,
            "states": spec["states"],
            "actionable": is_act,
            "box": spec["box"],
        })

    ax_sha, ax_stable = _compute_ax_hashes(out_nodes)
    actionable_ids = [n["id"] for n in out_nodes if n["actionable"]]
    state = {
        "schema": schema.SCHEMA_ID,
        "source": {"kind": kind, "url": f"https://example.test/{seed_node.seed & 0xFFFF:04x}", "title": f"Page {seed_node.seed & 0xFF}"},
        "prune": {"name": "prune_ax_v1", "version": 1},
        "ax_sha256": ax_sha,
        "ax_stable_sha256": ax_stable,
        "viewport": viewport,
        "nodes": out_nodes,
        "actionable": actionable_ids,
        "counts": {
            "raw": len(out_nodes) + rng.randint(0, 5),
            "nodes": len(out_nodes),
            "actionable": len(actionable_ids),
        },
    }
    return state


def generate_action_sequence(seed_node, *, min_len=2, max_len=6):
    """Generate a valid (state, actions) fixture from a SeedTree node."""
    tree_seed = seed_node.child("tree", 0)
    seq_seed = seed_node.child("seq", 0)
    state = generate_ui_tree(tree_seed, min_nodes=3, max_nodes=8, ensure_actionable=True)
    rng = seq_seed.rng()

    vp = state["viewport"]
    sx, sy, vw, vh = vp["scroll_x"], vp["scroll_y"], vp["w"], vp["h"]
    plannable_ids = [n["id"] for n in state["nodes"] if n["actionable"] and n["box"] is not None]
    if not plannable_ids:
        plannable_ids = [state["nodes"][0]["id"]]

    length = rng.randint(max(1, min_len), max(min_len, max_len))
    actions = []
    ops = ("click", "hover", "lpath_click", "type")
    for step_idx in range(length):
        op = rng.choice(ops)
        target_id = rng.choice(plannable_ids)
        if op == "click":
            actions.append({"op": "click", "target": target_id, "irreversible": False})
        elif op == "hover":
            actions.append({"op": "hover", "target": target_id, "irreversible": False})
        elif op == "lpath_click":
            ox = rng.randint(sx, sx + vw - 1)
            oy = rng.randint(sy, sy + vh - 1)
            policy = rng.choice(("horizontal_first", "vertical_first"))
            actions.append({
                "op": "lpath_click",
                "target": target_id,
                "origin": [ox, oy],
                "irreversible": False,
                "waypoint_policy": policy,
            })
        else:
            text_len = rng.randint(1, 16)
            alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
            text = "".join(rng.choice(alphabet) for _ in range(text_len))
            actions.append({"op": "type", "target": target_id, "text": text, "irreversible": False})

    return {"state": state, "actions": actions}


DOMAIN_GENERATORS = {
    "ui_tree": generate_ui_tree,
    "bbox": generate_bbox,
    "dpr_scale": generate_dpr_scale,
    "action_sequence": generate_action_sequence,
}


# @anchor:wa.learn.property_fuzzer.shrinker
def _shrink_int_towards(val, target=0):
    """Yield smaller integer candidates moving from `val` toward `target`."""
    if val == target:
        return []
    candidates = [target]
    diff = val - target
    step = diff // 2
    while step != 0:
        cand = val - step
        if cand != val and cand not in candidates:
            candidates.append(cand)
        step //= 2
    if val > target and (val - 1) not in candidates:
        candidates.append(val - 1)
    elif val < target and (val + 1) not in candidates:
        candidates.append(val + 1)
    return candidates


def _propose_bbox_shrinks(box):
    x, y, w, h = box
    proposals = [[0, 0, 1, 1], [0, 0, w, h], [x, y, 1, 1]]
    for nx in _shrink_int_towards(x, 0):
        proposals.append([nx, y, w, h])
    for ny in _shrink_int_towards(y, 0):
        proposals.append([x, ny, w, h])
    for nw in _shrink_int_towards(w, 1):
        if nw >= 1:
            proposals.append([x, y, nw, h])
    for nh in _shrink_int_towards(h, 1):
        if nh >= 1:
            proposals.append([x, y, w, nh])
    return proposals


def _propose_dpr_shrinks(case):
    if _is_int(case):
        proposals = [1, 1000000]
        proposals.extend(c for c in _shrink_int_towards(case, 1) if c > 0)
        return proposals
    x, y, dpr = case["x"], case["y"], case["dpr_ppm"]
    proposals = [
        {"x": 0, "y": 0, "dpr_ppm": 1},
        {"x": 0, "y": 0, "dpr_ppm": 1000000},
        {"x": 0, "y": 0, "dpr_ppm": dpr},
        {"x": x, "y": y, "dpr_ppm": 1},
        {"x": x, "y": y, "dpr_ppm": 1000000},
    ]
    for nx in _shrink_int_towards(x, 0):
        proposals.append({"x": nx, "y": y, "dpr_ppm": dpr})
    for ny in _shrink_int_towards(y, 0):
        proposals.append({"x": x, "y": ny, "dpr_ppm": dpr})
    for ndpr in _shrink_int_towards(dpr, 1):
        if ndpr > 0:
            proposals.append({"x": x, "y": y, "dpr_ppm": ndpr})
    return proposals


def _subtree_ids(nodes, root_id):
    removed = {root_id}
    changed = True
    while changed:
        changed = False
        for n in nodes:
            if n["parent"] in removed and n["id"] not in removed:
                removed.add(n["id"])
                changed = True
    return removed


def prune_ui_subtree(state, target_node_id):
    """Return a new valid STATE with `target_node_id` and its descendants removed."""
    nodes = state["nodes"]
    if not nodes or nodes[0]["id"] == target_node_id:
        raise FuzzerError("cannot prune root node of UI tree")
    to_remove = _subtree_ids(nodes, target_node_id)
    if not to_remove:
        return deepcopy(state)
    surviving = [deepcopy(n) for n in nodes if n["id"] not in to_remove]
    candidate = deepcopy(state)
    candidate["nodes"] = surviving
    return rebuild_valid_state_metadata(candidate)


def _propose_ui_tree_shrinks(state):
    proposals = []
    nodes = state["nodes"]
    if len(nodes) > 1:
        non_root_ids = [n["id"] for n in nodes[1:]]
        scored = sorted(
            non_root_ids,
            key=lambda nid: (len(_subtree_ids(nodes, nid)), nodes[[m["id"] for m in nodes].index(nid)]["depth"]),
            reverse=True,
        )
        for nid in scored:
            proposals.append(prune_ui_subtree(state, nid))

    vp = state.get("viewport") or {"w": 320, "h": 240, "scroll_x": 0, "scroll_y": 0}
    sx = vp.get("scroll_x", 0)
    sy = vp.get("scroll_y", 0)
    for idx, n in enumerate(nodes):
        if n["states"]:
            cand = deepcopy(state)
            cand["nodes"][idx]["states"] = {}
            if cand["nodes"][idx]["role"] in _ACTIONABLE_ROLES:
                cand["nodes"][idx]["actionable"] = True
            proposals.append(rebuild_valid_state_metadata(cand))
        if len(n["name"]) > 1:
            cand = deepcopy(state)
            cand["nodes"][idx]["name"] = n["name"][:1]
            proposals.append(rebuild_valid_state_metadata(cand))
        box = n["box"]
        if isinstance(box, list) and box != [sx, sy, 1, 1]:
            cand = deepcopy(state)
            cand["nodes"][idx]["box"] = [sx, sy, 1, 1]
            proposals.append(rebuild_valid_state_metadata(cand))
    return proposals


def _propose_action_sequence_shrinks(fixture):
    proposals = []
    state = fixture["state"]
    actions = fixture["actions"]
    n = len(actions)

    if n > 1:
        half = max(1, n // 2)
        proposals.append({"state": deepcopy(state), "actions": deepcopy(actions[:half])})
        proposals.append({"state": deepcopy(state), "actions": deepcopy(actions[n - half:])})
        for i in range(n):
            proposals.append({"state": deepcopy(state), "actions": [deepcopy(actions[i])]})
        for i in range(n):
            reduced = deepcopy(actions[:i] + actions[i + 1:])
            if reduced:
                proposals.append({"state": deepcopy(state), "actions": reduced})

    referenced_ids = {a.get("target") for a in actions}
    if len(state["nodes"]) > 1:
        for node in reversed(state["nodes"][1:]):
            sub_ids = _subtree_ids(state["nodes"], node["id"])
            if not (sub_ids & referenced_ids):
                pruned_state = prune_ui_subtree(state, node["id"])
                proposals.append({"state": pruned_state, "actions": deepcopy(actions)})

    vp = state.get("viewport") or {"w": 320, "h": 240, "scroll_x": 0, "scroll_y": 0}
    sx, sy = vp.get("scroll_x", 0), vp.get("scroll_y", 0)
    for i, act in enumerate(actions):
        if act["op"] == "type" and len(act.get("text", "")) > 1:
            for shorter in ("a", act["text"][:len(act["text"]) // 2]):
                cand_actions = deepcopy(actions)
                cand_actions[i]["text"] = shorter
                proposals.append({"state": deepcopy(state), "actions": cand_actions})
        elif act["op"] == "lpath_click":
            ox, oy = act["origin"]
            if [ox, oy] != [sx, sy]:
                cand_actions = deepcopy(actions)
                cand_actions[i]["origin"] = [sx, sy]
                proposals.append({"state": deepcopy(state), "actions": cand_actions})
            for nx in _shrink_int_towards(ox, sx):
                cand_actions = deepcopy(actions)
                cand_actions[i]["origin"] = [nx, oy]
                proposals.append({"state": deepcopy(state), "actions": cand_actions})
            for ny in _shrink_int_towards(oy, sy):
                cand_actions = deepcopy(actions)
                cand_actions[i]["origin"] = [ox, ny]
                proposals.append({"state": deepcopy(state), "actions": cand_actions})

    for tree_cand in _propose_ui_tree_shrinks(state):
        proposals.append({"state": tree_cand, "actions": deepcopy(actions)})

    return proposals


DOMAIN_PROPOSERS = {
    "ui_tree": _propose_ui_tree_shrinks,
    "bbox": _propose_bbox_shrinks,
    "dpr_scale": _propose_dpr_shrinks,
    "action_sequence": _propose_action_sequence_shrinks,
}


def fixture_measure(domain, fixture):
    """Return a lexicographic complexity tuple (length/subtree_size, magnitude) for a domain fixture."""
    if domain == "bbox":
        x, y, w, h = fixture
        return (1, abs(x) + abs(y) + w + h)
    if domain == "dpr_scale":
        if _is_int(fixture):
            return (1, abs(fixture - 1))
        return (1, abs(fixture["x"]) + abs(fixture["y"]) + abs(fixture["dpr_ppm"] - 1))
    if domain == "ui_tree":
        nodes = fixture["nodes"]
        box_mag = sum(
            abs(n["box"][0]) + abs(n["box"][1]) + n["box"][2] + n["box"][3]
            for n in nodes
            if isinstance(n.get("box"), list)
        )
        state_mag = sum(len(n.get("states", {})) + len(n.get("name", "")) for n in nodes)
        return (len(nodes), box_mag + state_mag)
    if domain == "action_sequence":
        actions = fixture["actions"]
        state_nodes = len(fixture["state"]["nodes"])
        act_mag = 0
        for a in actions:
            if a["op"] == "type":
                act_mag += len(a.get("text", ""))
            elif a["op"] == "lpath_click":
                act_mag += abs(a["origin"][0]) + abs(a["origin"][1])
        return (len(actions), state_nodes, act_mag)
    raise FuzzerError(f"unknown domain: {domain!r}")


def hierarchical_shrink(
    domain,
    initial_fixture,
    failing_predicate,
    *,
    validity_fn=None,
    max_attempts=MAX_SHRINK_ATTEMPTS,
    root_seed=0,
    domain_seed=0,
    case_seed=0,
    case_index=0,
    predicate_name="failing_predicate",
    path=("root",),
):
    """Bounded hierarchical shrinker preserving validity class and failing predicate.

    Evaluates at most `max_attempts` (default 32) validity-preserving candidate fixtures.
    """
    if domain not in DOMAINS:
        raise FuzzerError(f"unknown domain {domain!r}")
    if not _is_int(max_attempts) or max_attempts < 1 or max_attempts > MAX_SHRINK_ATTEMPTS:
        raise FuzzerError(f"max_attempts must be an int in 1..{MAX_SHRINK_ATTEMPTS}")

    validator = validity_fn or DOMAIN_VALIDATORS[domain]
    proposer = DOMAIN_PROPOSERS[domain]

    if not validator(initial_fixture):
        raise FuzzerError("initial_fixture does not belong to the domain validity class")
    if not failing_predicate(deepcopy(initial_fixture)):
        raise FuzzerError("initial_fixture does not satisfy failing_predicate")

    current = deepcopy(initial_fixture)
    current_score = fixture_measure(domain, current)
    attempts = 0
    accepted_steps = 0
    seen_dumps = {schema.dumps(current)}

    while attempts < max_attempts:
        improved = False
        for candidate in proposer(current):
            if attempts >= max_attempts:
                break
            try:
                cand_dump = schema.dumps(candidate)
            except Exception:
                continue
            if cand_dump in seen_dumps:
                continue
            seen_dumps.add(cand_dump)
            if not validator(candidate):
                continue
            if fixture_measure(domain, candidate) >= current_score:
                continue
            attempts += 1
            if failing_predicate(deepcopy(candidate)):
                current = deepcopy(candidate)
                current_score = fixture_measure(domain, current)
                accepted_steps += 1
                improved = True
                break
        if not improved:
            break

    repro = bool(validator(current) and failing_predicate(deepcopy(current)))
    return CounterexampleMetadata(
        domain=domain,
        root_seed=root_seed,
        domain_seed=domain_seed,
        case_seed=case_seed,
        case_index=case_index,
        validity_class=f"valid_{domain}",
        predicate_name=predicate_name,
        original_fixture=deepcopy(initial_fixture),
        minimal_fixture=deepcopy(current),
        shrink_attempts=attempts,
        shrink_steps=accepted_steps,
        reproducible=repro,
        path=tuple(path),
    )


def _property_fails(property_fn, fixture):
    """Return True if `property_fn(fixture)` returns False or raises AssertionError."""
    try:
        res = property_fn(deepcopy(fixture))
        return res is False
    except AssertionError:
        return True


# @anchor:wa.learn.property_fuzzer.run_domain_campaign
def run_domain_campaign(
    root_seed,
    domain,
    property_fn,
    *,
    cases_per_domain=DEFAULT_CASES_PER_DOMAIN,
    max_shrink_attempts=MAX_SHRINK_ATTEMPTS,
    max_generation_Multiple=4,
    generator_fn=None,
    validity_fn=None,
):
    """Run a seeded stdlib random property campaign targeting `cases_per_domain` (64) valid cases.

    If a failing case is discovered, shrinks it in at most `max_shrink_attempts` (32)
    validity-preserving attempts and returns a report dict with the minimal counterexample.
    If the generation budget is exhausted before reaching `cases_per_domain` valid cases,
    raises `FuzzerBudgetExhausted` (budget exhaustion is incomplete).
    """
    if not _is_int(cases_per_domain) or cases_per_domain < 1:
        raise FuzzerError("cases_per_domain must be a positive int")
    if not _is_int(max_shrink_attempts) or max_shrink_attempts < 1 or max_shrink_attempts > MAX_SHRINK_ATTEMPTS:
        raise FuzzerError(f"max_shrink_attempts must be in 1..{MAX_SHRINK_ATTEMPTS}")

    tree = SeedTree(root_seed)
    domain_node = tree.domain(domain)
    gen = generator_fn or DOMAIN_GENERATORS[domain]
    validator = validity_fn or DOMAIN_VALIDATORS[domain]

    max_draws = cases_per_domain * max(1, int(max_generation_Multiple))
    valid_count = 0
    draw_index = 0

    while valid_count < cases_per_domain and draw_index < max_draws:
        case_node = domain_node.child("case", draw_index)
        draw_index += 1
        fixture = gen(case_node)
        if not validator(fixture):
            continue
        case_idx = valid_count
        valid_count += 1

        if _property_fails(property_fn, fixture):
            meta = hierarchical_shrink(
                domain,
                fixture,
                lambda cand: _property_fails(property_fn, cand),
                validity_fn=validator,
                max_attempts=max_shrink_attempts,
                root_seed=tree.seed,
                domain_seed=domain_node.seed,
                case_seed=case_node.seed,
                case_index=case_idx,
                predicate_name=getattr(property_fn, "__name__", "property_fn"),
                path=case_node.path,
            )
            return {
                "status": "counterexample",
                "domain": domain,
                "root_seed": tree.seed,
                "domain_seed": domain_node.seed,
                "case_seed": case_node.seed,
                "case_index": case_idx,
                "valid_cases": valid_count,
                "draws": draw_index,
                "minimal_fixture": meta.minimal_fixture,
                "original_fixture": meta.original_fixture,
                "shrink_attempts": meta.shrink_attempts,
                "shrink_steps": meta.shrink_steps,
                "counterexample": meta,
            }

    if valid_count < cases_per_domain:
        raise FuzzerBudgetExhausted(
            f"domain {domain!r} budget exhausted: generated {valid_count}/{cases_per_domain} valid cases in {draw_index} draws"
        )

    return {
        "status": "ok",
        "domain": domain,
        "root_seed": tree.seed,
        "domain_seed": domain_node.seed,
        "case_seed": None,
        "case_index": None,
        "valid_cases": valid_count,
        "draws": draw_index,
        "minimal_fixture": None,
        "original_fixture": None,
        "shrink_attempts": 0,
        "shrink_steps": 0,
        "counterexample": None,
    }


def require_future_collaborator(module_path):
    """Block lazily on absent sibling deliverables rather than fabricating their APIs."""
    if not isinstance(module_path, str) or not module_path.strip():
        raise FuzzerError("module_path must be a non-empty string")
    try:
        __import__(module_path)
    except ModuleNotFoundError as exc:
        raise RuntimeError(f"AUSENTE sibling dependency blocked on lazy import: {module_path}") from exc
    return __import__(module_path, fromlist=["*"])
