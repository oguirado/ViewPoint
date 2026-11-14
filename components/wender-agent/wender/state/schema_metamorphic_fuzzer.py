# @module {"id":"wa.state.schema_metamorphic_fuzzer","layer":1,"owner":"STATE","deps":["wa.state.schema","wa.state.diff","wa.act.coordinates"],"book":"HB-WA-a0-arch"}
"""@module wa.state.schema_metamorphic_fuzzer - metamorphic fuzzer for STATE schema and diff.

Checks schema and diff metamorphisms across valid node permutations preserving parent and
actionable order, DPR scaling, subtree pruning, and canonical dumps with independent
caller-computed SHA-256 expectations. Reports reproducible seed, invariant, and smallest
validity-preserving fixture.
"""
from copy import deepcopy
import dataclasses
from dataclasses import dataclass
import hashlib
import json
import random

from wender.act import coordinates
from wender.state import schema
from wender.state.diff import diff_states, summarize

MASK64 = 0xFFFFFFFFFFFFFFFF
GOLDEN_GAMMA = 0x9E3779B97F4A7C15
MIX1 = 0xBF58476D1CE4E5B9
MIX2 = 0x94D049BB133111EB

INVARIANTS = (
    "node_permutation",
    "dpr_scaling",
    "subtree_pruning",
    "canonical_dumps_sha256",
)
DEFAULT_CASES_PER_INVARIANT = 64
MAX_SHRINK_ATTEMPTS = 32

_NON_ACTIONABLE_ROLES = ("region", "group", "main", "navigation", "form", "dialog")
_ACTIONABLE_ROLES = ("button", "link", "textbox", "checkbox", "radio", "switch", "tab")
_STATE_ATTRS = ("busy", "checked", "current", "disabled", "expanded", "hidden", "pressed", "selected")


class MetamorphicError(ValueError):
    """Raised when metamorphic fuzzer inputs or budgets are invalid."""


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _mix64(z):
    z = z & MASK64
    z = ((z ^ (z >> 30)) * MIX1) & MASK64
    z = ((z ^ (z >> 27)) * MIX2) & MASK64
    return (z ^ (z >> 31)) & MASK64


def derive_seed(root_seed, invariant, case_index=0):
    """Deterministically derive a 64-bit seed from (root_seed, invariant, case_index)."""
    if not _is_int(root_seed) or root_seed < 0:
        raise MetamorphicError("root_seed must be a non-negative int")
    if not isinstance(invariant, str) or not invariant:
        raise MetamorphicError("invariant must be a non-empty string")
    if not _is_int(case_index) or case_index < 0:
        raise MetamorphicError("case_index must be a non-negative int")
    acc = _mix64(root_seed ^ GOLDEN_GAMMA)
    for b in invariant.encode("utf-8"):
        acc = _mix64(acc ^ b)
    return _mix64(acc ^ ((case_index + 1) * MIX1))


@dataclass(frozen=True)
class MetamorphicCounterexample:
    """Reproducible metamorphic failure report with smallest validity-preserving fixture."""
    seed: int
    case_seed: int
    case_index: int
    invariant: str
    original_fixture: dict
    minimal_fixture: dict
    shrink_attempts: int
    shrink_steps: int
    reproducible: bool


def caller_sha256(text):
    """Compute SHA-256 hex digest at the caller from a canonical JSON string."""
    if not isinstance(text, str):
        raise MetamorphicError("canonical JSON input must be a str")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def independent_canonical_json(obj):
    """Independent reference canonical JSON serializer without calling `schema.dumps`."""
    if isinstance(obj, float):
        raise schema.StateError("float rejected in independent canonical encoder")
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if _is_int(obj):
        return str(obj)
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        as_dict = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
        return independent_canonical_json(as_dict)
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(independent_canonical_json(item) for item in obj) + "]"
    if isinstance(obj, dict):
        for k in obj.keys():
            if isinstance(k, float):
                raise schema.StateError("float key rejected")
        items = []
        for k in sorted(obj.keys()):
            if not isinstance(k, str):
                raise TypeError("JSON keys must be strings")
            items.append(json.dumps(k, ensure_ascii=False) + ":" + independent_canonical_json(obj[k]))
        return "{" + ",".join(items) + "}"
    raise TypeError(f"Unsupported type: {type(obj).__name__}")


def independent_sha256(obj):
    """Compute independent SHA-256 expectation over independent canonical JSON bytes."""
    return hashlib.sha256(independent_canonical_json(obj).encode("utf-8")).hexdigest()


def shuffle_dict_key_order(obj, seed=1):
    """Return a deep copy of `obj` with dictionary keys inserted in permuted order."""
    rng = random.Random(seed)

    def _walk(val):
        if isinstance(val, dict):
            keys = list(val.keys())
            if len(keys) > 1:
                reversed_keys = list(reversed(keys))
                rng.shuffle(keys)
                if keys == list(val.keys()):
                    keys = reversed_keys
            return {k: _walk(val[k]) for k in keys}
        if isinstance(val, list):
            return [_walk(x) for x in val]
        return deepcopy(val)

    return _walk(obj)


def _node_id_from_parts(ancestors, role, name, ordinal):
    base = "/".join(ancestors) + "\0" + role + "\0" + name + "\0"
    stable = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]
    return hashlib.sha256(f"{stable}\0{ordinal}".encode("utf-8")).hexdigest()[:16]


def _ax_hashes_for_nodes(nodes):
    if not nodes:
        empty = hashlib.sha256(b"").hexdigest()
        return empty, empty
    with_state = []
    without_state = []
    for n in nodes:
        st = n.get("states") or {}
        attrs = ",".join(f"{k}={st[k]}" for k in sorted(st))
        with_state.append(f"{n['depth']}|{n['role']}|{n['name']}|{attrs}")
        without_state.append(f"{n['depth']}|{n['role']}|{n['name']}|")
    return (
        hashlib.sha256(("\n".join(with_state) + "\n").encode("utf-8")).hexdigest(),
        hashlib.sha256(("\n".join(without_state) + "\n").encode("utf-8")).hexdigest(),
    )


def generate_metamorphic_state(seed, *, min_nodes=4, max_nodes=10):
    """Generate a valid STATE with both actionable and non-actionable sibling branches."""
    if not _is_int(seed) or seed < 0:
        raise MetamorphicError("seed must be a non-negative int")
    rng = random.Random(seed)
    kind = rng.choice(schema.SOURCE_KINDS)
    vw = rng.randint(400, 1400)
    vh = rng.randint(300, 1000)
    sx = rng.randint(0, 100)
    sy = rng.randint(0, 100)
    viewport = {"w": vw, "h": vh, "scroll_x": sx, "scroll_y": sy}

    total_nodes = rng.randint(max(3, min_nodes), max(min_nodes, max_nodes))
    specs = [
        {"role": "RootWebArea", "parent_idx": None, "depth": 0, "anc": (), "actionable": False},
        {"role": rng.choice(_NON_ACTIONABLE_ROLES), "parent_idx": 0, "depth": 1, "anc": ("RootWebArea",), "actionable": False},
        {"role": rng.choice(_NON_ACTIONABLE_ROLES), "parent_idx": 0, "depth": 1, "anc": ("RootWebArea",), "actionable": False},
    ]
    for idx in range(3, total_nodes):
        parent_idx = rng.randint(0, idx - 1)
        p_spec = specs[parent_idx]
        is_act = (idx == 3) or (rng.randint(0, 1) == 1)
        role = rng.choice(_ACTIONABLE_ROLES if is_act else _NON_ACTIONABLE_ROLES)
        specs.append({
            "role": role,
            "parent_idx": parent_idx,
            "depth": p_spec["depth"] + 1,
            "anc": p_spec["anc"] + (p_spec["role"],),
            "actionable": is_act,
        })

    counts_by_rn = {}
    nodes = []
    for idx, sp in enumerate(specs):
        role = sp["role"]
        name = f"{role}_{idx}_{rng.randint(10, 99)}"
        rn = (role, name)
        ordinal = counts_by_rn.get(rn, 0)
        counts_by_rn[rn] = ordinal + 1
        nid = _node_id_from_parts(sp["anc"], role, name, ordinal)
        while any(existing["id"] == nid for existing in nodes):
            ordinal += 1
            counts_by_rn[rn] = ordinal + 1
            nid = _node_id_from_parts(sp["anc"], role, name, ordinal)
        states = {}
        if rng.randint(0, 1) == 1:
            k = rng.choice(_STATE_ATTRS)
            if sp["actionable"] and k == "disabled":
                states[k] = "false"
            else:
                states[k] = rng.choice(("true", "false"))
        bw = rng.randint(6, 80)
        bh = rng.randint(6, 60)
        bx = rng.randint(sx, sx + vw - bw - 1)
        by = rng.randint(sy, sy + vh - bh - 1)
        parent_id = nodes[sp["parent_idx"]]["id"] if sp["parent_idx"] is not None else None
        nodes.append({
            "id": nid,
            "role": role,
            "name": name,
            "depth": sp["depth"],
            "parent": parent_id,
            "ordinal": ordinal,
            "states": states,
            "actionable": bool(sp["actionable"] and states.get("disabled") != "true"),
            "box": [bx, by, bw, bh],
        })

    ax_sha, ax_stable = _ax_hashes_for_nodes(nodes)
    actionable = [n["id"] for n in nodes if n["actionable"]]
    return {
        "schema": schema.SCHEMA_ID,
        "source": {"kind": kind, "url": f"https://metamorphic.test/{seed & 0xFFFF:04x}", "title": "Metamorphic"},
        "prune": {"name": "prune_ax_v1", "version": 1},
        "ax_sha256": ax_sha,
        "ax_stable_sha256": ax_stable,
        "viewport": viewport,
        "nodes": nodes,
        "actionable": actionable,
        "counts": {
            "raw": len(nodes) + 2,
            "nodes": len(nodes),
            "actionable": len(actionable),
        },
    }


def is_valid_metamorphic_state(state, *, min_nodes=2):
    """Return True iff `state` is valid under `schema.validate_state` with at least `min_nodes`."""
    if not isinstance(state, dict):
        return False
    if schema.validate_state(state) != []:
        return False
    return len(state.get("nodes", [])) >= min_nodes


# @anchor:wa.state.schema_metamorphic_fuzzer.transforms
def permute_nodes_preserving_order(state, seed=0):
    """Permute `state['nodes']` while preserving parent-before-child and actionable relative order."""
    nodes = [deepcopy(n) for n in state["nodes"]]
    if len(nodes) <= 2:
        return deepcopy(state)

    by_id = {n["id"]: n for n in nodes}
    orig_index = {n["id"]: i for i, n in enumerate(nodes)}
    actionable_ids = [n["id"] for n in nodes if n["actionable"]]

    deps = {n["id"]: set() for n in nodes}
    for n in nodes:
        if n["parent"] is not None:
            deps[n["id"]].add(n["parent"])
    for i in range(len(actionable_ids) - 1):
        deps[actionable_ids[i + 1]].add(actionable_ids[i])

    rng = random.Random(seed)
    placed = []
    placed_set = set()
    remaining = [n["id"] for n in nodes]

    while remaining:
        ready = [nid for nid in remaining if deps[nid].issubset(placed_set)]
        if not ready:
            ready = [remaining[0]]
        ready.sort(key=lambda nid: orig_index[nid], reverse=True)
        chosen = ready[0] if rng.randint(0, 1) >= 0 else ready[-1]
        placed.append(by_id[chosen])
        placed_set.add(chosen)
        remaining.remove(chosen)

    out = deepcopy(state)
    out["nodes"] = placed
    return out


def scale_state_dpr(state, dpr_ppm):
    """Return a new valid STATE with node boxes scaled by `dpr_ppm` via `coordinates.css_to_device`."""
    if not _is_int(dpr_ppm) or dpr_ppm <= 0:
        raise MetamorphicError("dpr_ppm must be a positive int")
    out = deepcopy(state)
    if out.get("viewport") is not None:
        vp = out["viewport"]
        vw, vh = coordinates.css_to_device(vp["w"], vp["h"], dpr_ppm)
        sx, sy = coordinates.css_to_device(vp.get("scroll_x", 0), vp.get("scroll_y", 0), dpr_ppm)
        out["viewport"] = {
            "w": max(1, vw),
            "h": max(1, vh),
            "scroll_x": sx,
            "scroll_y": sy,
        }
    for n in out["nodes"]:
        box = n.get("box")
        if isinstance(box, list) and len(box) == 4:
            x, y, w, h = box
            dx, dy = coordinates.css_to_device(x, y, dpr_ppm)
            dw, dh = coordinates.css_to_device(w, h, dpr_ppm)
            n["box"] = [dx, dy, max(1, dw), max(1, dh)]
    return out


def prune_state_subtree(state, root_node_id=None):
    """Prune the closed subtree rooted at `root_node_id` (defaults to last non-root branch)."""
    nodes = state["nodes"]
    if len(nodes) < 2:
        raise MetamorphicError("cannot prune subtree from state with fewer than 2 nodes")
    if root_node_id is None:
        root_node_id = nodes[-1]["id"]
        for cand in nodes[1:]:
            if any(other["parent"] == cand["id"] for other in nodes):
                root_node_id = cand["id"]
                break
    if root_node_id == nodes[0]["id"]:
        raise MetamorphicError("cannot prune root node")

    removed_set = {root_node_id}
    changed = True
    while changed:
        changed = False
        for n in nodes:
            if n["parent"] in removed_set and n["id"] not in removed_set:
                removed_set.add(n["id"])
                changed = True

    pruned_ids = [n["id"] for n in nodes if n["id"] in removed_set]
    surviving_nodes = [deepcopy(n) for n in nodes if n["id"] not in removed_set]
    after = deepcopy(state)
    after["nodes"] = surviving_nodes
    after["actionable"] = [n["id"] for n in surviving_nodes if n["actionable"]]
    after["counts"] = {
        "raw": state["counts"]["raw"],
        "nodes": len(surviving_nodes),
        "actionable": len(after["actionable"]),
    }
    return after, pruned_ids


# @anchor:wa.state.schema_metamorphic_fuzzer.checks
def check_node_permutation_metamorphism(state, *, transform_fn=None):
    """Check schema and diff invariants under parent- and actionable-preserving node permutation."""
    if schema.validate_state(state) != []:
        return False
    permuted = (transform_fn or permute_nodes_preserving_order)(deepcopy(state))
    if schema.validate_state(permuted) != []:
        return False
    if permuted["actionable"] != state["actionable"]:
        return False
    if permuted["counts"] != state["counts"]:
        return False
    d = diff_states(state, permuted)
    if (
        d["added"] != []
        or d["removed"] != []
        or d["changed"] != []
        or d["layout_only"] != []
        or d["states_flipped"] != {}
        or d["actionable_gained"] != []
        or d["actionable_lost"] != []
        or d["same_chassis"] is not True
        or d["counts"] != {"added": 0, "removed": 0, "changed": 0}
    ):
        return False
    payload = {k: v for k, v in d.items() if k != "diff_sha256"}
    return d["diff_sha256"] == independent_sha256(payload) and summarize(d) == "plus 0 minus 0 changed 0"


def check_dpr_scaling_metamorphism(state, *, dpr_ppm=1500000, transform_fn=None):
    """Check descriptor envelope, coordinate planner, and layout-only diff invariants under DPR scaling."""
    if schema.validate_state(state) != []:
        return False
    handle = schema.Handle(kind="cdp_tab", canonical_id="tab-meta-1", generation=1)
    env_base = schema.build_target_descriptor(handle, epoch=1, state=state, frame_seq=0, dpr_ppm=1000000)
    env_scaled = schema.build_target_descriptor(handle, epoch=1, state=state, frame_seq=0, dpr_ppm=dpr_ppm)
    if schema.validate_target_descriptor(env_base) != [] or schema.validate_target_descriptor(env_scaled) != []:
        return False
    if caller_sha256(schema.dumps(env_base["state"])) != caller_sha256(schema.dumps(env_scaled["state"])):
        return False

    if state["actionable"]:
        act_id = state["actionable"][0]
        if coordinates.plan_click(env_base["state"], act_id) != coordinates.plan_click(env_scaled["state"], act_id):
            return False

    scaled_state = (transform_fn or (lambda s: scale_state_dpr(s, dpr_ppm)))(deepcopy(state))
    if schema.validate_state(scaled_state) != []:
        return False
    d = diff_states(state, scaled_state)
    old_boxes = {n["id"]: n["box"] for n in state["nodes"]}
    expected_layout_only = [
        n["id"] for n in scaled_state["nodes"] if n["box"] != old_boxes.get(n["id"])
    ]
    if (
        d["added"] != []
        or d["removed"] != []
        or d["changed"] != []
        or d["layout_only"] != expected_layout_only
        or d["states_flipped"] != {}
        or d["actionable_gained"] != []
        or d["actionable_lost"] != []
        or d["same_chassis"] is not True
    ):
        return False
    payload = {k: v for k, v in d.items() if k != "diff_sha256"}
    return d["diff_sha256"] == independent_sha256(payload)


def check_subtree_pruning_metamorphism(state, *, prune_fn=None):
    """Check schema validity and forward/reverse diff invariants when pruning a UI subtree."""
    if not is_valid_metamorphic_state(state, min_nodes=2):
        return False
    after, pruned_ids = (prune_fn or prune_state_subtree)(deepcopy(state))
    if schema.validate_state(after) != []:
        return False
    if not pruned_ids:
        return False

    old_by_id = {n["id"]: n for n in state["nodes"]}
    expected_lost_actionable = [nid for nid in pruned_ids if old_by_id[nid]["actionable"]]

    forward = diff_states(state, after)
    if (
        forward["removed"] != pruned_ids
        or forward["added"] != []
        or forward["changed"] != []
        or forward["layout_only"] != []
        or forward["states_flipped"] != {}
        or forward["actionable_gained"] != []
        or forward["actionable_lost"] != expected_lost_actionable
        or forward["counts"] != {"added": 0, "removed": len(pruned_ids), "changed": 0}
    ):
        return False

    reverse = diff_states(after, state)
    if (
        reverse["added"] != pruned_ids
        or reverse["removed"] != []
        or reverse["changed"] != []
        or reverse["layout_only"] != []
        or reverse["states_flipped"] != {}
        or reverse["actionable_gained"] != expected_lost_actionable
        or reverse["actionable_lost"] != []
        or reverse["counts"] != {"added": len(pruned_ids), "removed": 0, "changed": 0}
    ):
        return False

    f_payload = {k: v for k, v in forward.items() if k != "diff_sha256"}
    r_payload = {k: v for k, v in reverse.items() if k != "diff_sha256"}
    return (
        forward["diff_sha256"] == independent_sha256(f_payload)
        and reverse["diff_sha256"] == independent_sha256(r_payload)
    )


def check_canonical_dumps_sha256_metamorphism(state, *, dump_fn=None):
    """Check canonical JSON dumps and independent caller SHA-256 across key orderings and envelopes."""
    if schema.validate_state(state) != []:
        return False
    dumper = dump_fn or schema.dumps
    shuffled = shuffle_dict_key_order(state, seed=77)

    dumped_orig = dumper(state)
    dumped_shuf = dumper(shuffled)
    indep_json = independent_canonical_json(state)
    indep_digest = independent_sha256(state)

    if dumped_orig != dumped_shuf or dumped_orig != indep_json:
        return False
    if caller_sha256(dumped_orig) != indep_digest or caller_sha256(dumped_shuf) != indep_digest:
        return False
    if json.loads(dumped_orig) != state:
        return False

    h_dc = schema.Handle(kind="cdp_tab", canonical_id="canon-0", generation=2)
    h_dict = {"generation": 2, "canonical_id": "canon-0", "kind": "cdp_tab"}
    env_dc = schema.build_target_descriptor(h_dc, epoch=4, state=state, frame_seq=1, dpr_ppm=1250000)
    env_dict = schema.build_target_descriptor(h_dict, epoch=4, state=shuffled, frame_seq=1, dpr_ppm=1250000)
    if dumper(env_dc) != dumper(env_dict):
        return False
    if caller_sha256(dumper(env_dc)) != independent_sha256(env_dc):
        return False
    return True


INVARIANT_CHECKS = {
    "node_permutation": check_node_permutation_metamorphism,
    "dpr_scaling": check_dpr_scaling_metamorphism,
    "subtree_pruning": check_subtree_pruning_metamorphism,
    "canonical_dumps_sha256": check_canonical_dumps_sha256_metamorphism,
}


def _state_complexity(state):
    nodes = state["nodes"]
    box_mag = sum(
        abs(n["box"][0]) + abs(n["box"][1]) + n["box"][2] + n["box"][3]
        for n in nodes
        if isinstance(n.get("box"), list)
    )
    meta_mag = sum(len(n.get("states", {})) + len(n.get("name", "")) for n in nodes)
    return (len(nodes), box_mag + meta_mag)


def _propose_state_shrinks(state, min_nodes=2):
    proposals = []
    nodes = state["nodes"]
    if len(nodes) > min_nodes:
        for cand_node in reversed(nodes[1:]):
            after, _ = prune_state_subtree(state, cand_node["id"])
            if len(after["nodes"]) >= min_nodes:
                proposals.append(after)

    vp = state.get("viewport") or {"w": 400, "h": 300, "scroll_x": 0, "scroll_y": 0}
    sx, sy = vp.get("scroll_x", 0), vp.get("scroll_y", 0)
    for idx, n in enumerate(nodes):
        if n["states"]:
            cand = deepcopy(state)
            cand["nodes"][idx]["states"] = {}
            if cand["nodes"][idx]["role"] in _ACTIONABLE_ROLES:
                cand["nodes"][idx]["actionable"] = True
            cand["actionable"] = [m["id"] for m in cand["nodes"] if m["actionable"]]
            cand["counts"]["actionable"] = len(cand["actionable"])
            ax_sha, ax_stable = _ax_hashes_for_nodes(cand["nodes"])
            cand["ax_sha256"] = ax_sha
            cand["ax_stable_sha256"] = ax_stable
            proposals.append(cand)
        if len(n["name"]) > 1:
            cand = deepcopy(state)
            cand["nodes"][idx]["name"] = n["name"][:1]
            ax_sha, ax_stable = _ax_hashes_for_nodes(cand["nodes"])
            cand["ax_sha256"] = ax_sha
            cand["ax_stable_sha256"] = ax_stable
            proposals.append(cand)
        if isinstance(n.get("box"), list) and n["box"] != [sx, sy, 1, 1]:
            cand = deepcopy(state)
            cand["nodes"][idx]["box"] = [sx, sy, 1, 1]
            proposals.append(cand)
    return proposals


def shrink_metamorphic_counterexample(
    invariant,
    initial_state,
    failing_predicate,
    *,
    seed=0,
    case_seed=0,
    case_index=0,
    max_attempts=MAX_SHRINK_ATTEMPTS,
    min_nodes=2,
):
    """Shrink a failing STATE fixture in at most 32 validity-preserving attempts."""
    if invariant not in INVARIANTS:
        raise MetamorphicError(f"unknown invariant {invariant!r}")
    if not _is_int(max_attempts) or max_attempts < 1 or max_attempts > MAX_SHRINK_ATTEMPTS:
        raise MetamorphicError(f"max_attempts must be in 1..{MAX_SHRINK_ATTEMPTS}")
    if not is_valid_metamorphic_state(initial_state, min_nodes=min_nodes):
        raise MetamorphicError("initial_state is not a valid metamorphic fixture")
    if not failing_predicate(deepcopy(initial_state)):
        raise MetamorphicError("initial_state does not trigger failing_predicate")

    current = deepcopy(initial_state)
    current_score = _state_complexity(current)
    attempts = 0
    steps = 0
    seen = {schema.dumps(current)}

    while attempts < max_attempts:
        improved = False
        for cand in _propose_state_shrinks(current, min_nodes=min_nodes):
            if attempts >= max_attempts:
                break
            dumped = schema.dumps(cand)
            if dumped in seen:
                continue
            seen.add(dumped)
            if not is_valid_metamorphic_state(cand, min_nodes=min_nodes):
                continue
            if _state_complexity(cand) >= current_score:
                continue
            attempts += 1
            if failing_predicate(deepcopy(cand)):
                current = deepcopy(cand)
                current_score = _state_complexity(current)
                steps += 1
                improved = True
                break
        if not improved:
            break

    repro = bool(
        is_valid_metamorphic_state(current, min_nodes=min_nodes)
        and failing_predicate(deepcopy(current))
    )
    return MetamorphicCounterexample(
        seed=seed,
        case_seed=case_seed,
        case_index=case_index,
        invariant=invariant,
        original_fixture=deepcopy(initial_state),
        minimal_fixture=deepcopy(current),
        shrink_attempts=attempts,
        shrink_steps=steps,
        reproducible=repro,
    )


def _check_fails(check_fn, state):
    try:
        return check_fn(deepcopy(state)) is False
    except AssertionError:
        return True


# @anchor:wa.state.schema_metamorphic_fuzzer.run_metamorphic_campaign
def run_metamorphic_campaign(
    root_seed,
    invariant,
    *,
    cases_per_invariant=DEFAULT_CASES_PER_INVARIANT,
    max_shrink_attempts=MAX_SHRINK_ATTEMPTS,
    check_fn=None,
):
    """Run a seeded 64-case metamorphic campaign for `invariant` and shrink any counterexample."""
    if invariant not in INVARIANTS:
        raise MetamorphicError(f"unknown invariant {invariant!r}; expected one of {INVARIANTS!r}")
    if not _is_int(cases_per_invariant) or cases_per_invariant < 1:
        raise MetamorphicError("cases_per_invariant must be a positive int")
    if not _is_int(max_shrink_attempts) or max_shrink_attempts < 1 or max_shrink_attempts > MAX_SHRINK_ATTEMPTS:
        raise MetamorphicError(f"max_shrink_attempts must be in 1..{MAX_SHRINK_ATTEMPTS}")

    checker = check_fn or INVARIANT_CHECKS[invariant]
    min_nodes = 2 if invariant == "subtree_pruning" else 3
    valid_cases = 0
    draw_idx = 0
    max_draws = cases_per_invariant * 4

    while valid_cases < cases_per_invariant and draw_idx < max_draws:
        c_seed = derive_seed(root_seed, invariant, draw_idx)
        draw_idx += 1
        fixture = generate_metamorphic_state(c_seed, min_nodes=max(4, min_nodes), max_nodes=9)
        if not is_valid_metamorphic_state(fixture, min_nodes=min_nodes):
            continue
        case_idx = valid_cases
        valid_cases += 1

        if _check_fails(checker, fixture):
            ce = shrink_metamorphic_counterexample(
                invariant,
                fixture,
                lambda s: _check_fails(checker, s),
                seed=root_seed,
                case_seed=c_seed,
                case_index=case_idx,
                max_attempts=max_shrink_attempts,
                min_nodes=min_nodes,
            )
            return {
                "status": "counterexample",
                "seed": root_seed,
                "case_seed": c_seed,
                "case_index": case_idx,
                "invariant": invariant,
                "valid_cases": valid_cases,
                "minimal_fixture": ce.minimal_fixture,
                "original_fixture": ce.original_fixture,
                "shrink_attempts": ce.shrink_attempts,
                "shrink_steps": ce.shrink_steps,
                "counterexample": ce,
            }

    if valid_cases < cases_per_invariant:
        raise RuntimeError(f"metamorphic budget exhausted for {invariant!r}: {valid_cases}/{cases_per_invariant}")

    return {
        "status": "ok",
        "seed": root_seed,
        "case_seed": None,
        "case_index": None,
        "invariant": invariant,
        "valid_cases": valid_cases,
        "minimal_fixture": None,
        "original_fixture": None,
        "shrink_attempts": 0,
        "shrink_steps": 0,
        "counterexample": None,
    }
