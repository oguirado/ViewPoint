# @module {"id":"vm.compact","layer":5,"stage":"S7","owner":"VMEM","deps":["vc.canon","vc.errors","vc.ids","vm.tiers","vm.units"],"book":"HB-VC-s7-vmem"}
"""@module vm.compact - FG-preserving compaction, mark-and-sweep gc, FG monotone gate (SPEC_VMEM §7). Pure planning; S1 persists."""
import hashlib

from visioncore.errors import VqaError

from . import tiers, units as U

VM_GEN_KEEP = 4


def fg_ids_sha256(units):
    """Sole definition (gates.verify and ingest import it): sha256 over sorted FG ids, each newline-terminated."""
    ids_ = sorted(u["id"] for u in units if U.is_fg(u))
    return hashlib.sha256("".join(i + "\n" for i in ids_).encode()).hexdigest()


def g_fg_monotone(parent_units, child_units):
    """FG_ids(n) subset of FG_ids(n+1); rc 5 E_GATE otherwise."""
    missing = sorted({u["id"] for u in parent_units if U.is_fg(u)} - {u["id"] for u in child_units if U.is_fg(u)})
    if missing:
        raise VqaError("E_GATE", {"reason": "g_fg_monotone", "missing": missing[:20]})
    return True


def fg_bytes(units, blob_sizes=None):
    sizes = blob_sizes or {}
    total, seen = 0, set()
    for u in units:
        if not U.is_fg(u):
            continue
        total += len(U.dump_unit(u).encode("utf-8"))
        for b in U.blob_refs(u):
            if b not in seen:
                seen.add(b)
                total += sizes.get(b, 0)
    return total


def _run_order(units_):
    runs = {}
    for u in units_:
        r = U.unit_run(u)
        if r:
            runs[r] = u["scope"]["build_seq"] or 0
    return sorted(runs, key=lambda r: (runs[r], r))


def _strip_blobs(u, drop_look=False):
    a = [dict(x, crop=None, **({"desc_blob": None, "gop_blob": None} if drop_look else {})) for x in u["anchors"]]
    lk = u["look"]  # retired BG episodes also drop gop_blob: gc frees it after the VM_GEN_KEEP window (SPEC_VMEM §7)
    if drop_look and lk:
        lk = dict(lk, desc_blob=None)
    return dict(u, anchors=a, look=lk)


def compact(units_, blob_sizes=None, k_clean=None, table=None, gen=None):  # @anchor:vm.compact.compact
    """-> dict(active, archive, retired, summaries, live_blobs, dropped_blobs, fg_ids_sha256).

    Per scope: runs older than the newest k_clean clean runs lose their BG all-PASS episode units
    (replaced by one @unchanged summary per (scope, build_seq)) and their BG crops. FG is never touched
    (root set). Input units are not mutated. fg_cap -> rc 5 E_GATE before any plan is returned."""
    t = table or tiers.DEFAULTS
    k = t["COMPACT_K_CLEAN"] if k_clean is None else k_clean
    if tiers.fg_state(fg_bytes(units_, blob_sizes), t) == "FG_BLOCK":
        raise VqaError("E_GATE", {"reason": "fg_cap"})
    gen = gen if gen is not None else max([u["last_gen"] for u in units_] or [0]) + 1
    dirty_runs = {U.unit_run(u) for u in units_ if u["pin"] == "FAIL" and U.unit_run(u)}
    by_scope = {}
    for u in units_:
        by_scope.setdefault(U.scope_key(u), []).append(u)
    old_runs = set()
    for us in by_scope.values():
        order = _run_order(us)
        clean = [r for r in order if r not in dirty_runs]
        if len(clean) > k:
            cut = clean[-k] if k else None  # oldest kept clean run
            old_runs |= {r for r in order if cut is None or order.index(r) < order.index(cut)}
    active, archive, retired, groups = [], [], [], {}
    for u in units_:
        r = U.unit_run(u)
        if U.is_fg(u) or r not in old_runs or u["status"] == "retired":
            (archive if u["status"] == "retired" else active).append(u)
            continue
        if u["kind"] == "episode" and u["verdict"] != "FAIL":
            retired.append(u["id"])
            archive.append(dict(_strip_blobs(u, True), status="retired", last_gen=max(gen, u["last_gen"])))
            groups.setdefault((U.scope_key(u), u["scope"]["build_seq"]), []).append((u, r))
        else:
            active.append(_strip_blobs(u))  # BG crop dropped; desc blob kept so look stays resolvable
    next_n = {}
    for u in units_:
        if u["kind"] == "episode":
            next_n[0] = max(next_n.get(0, 0), U.ids.parse_any(u["id"])[1]["n"])
    summaries = []
    for (sk, bs), members in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        proto = members[0][0]
        runs = sorted({r for _, r in members})
        next_n[0] += 1
        s = U.make_unit(next_n[0], "episode", proto["scope"], proto["tk"], predicate="baseline",
                        cls="UNCHANGED", gen=gen, value="@unchanged", value_num=len(runs),
                        receipt_sha256=proto["receipt_sha256"],
                        summary="@unchanged n_runs=%d runs_sha8=%s" % (
                            len(runs), hashlib.sha256("".join(runs).encode()).hexdigest()[:8]))
        summaries.append(s)
    active = U.sort_units(active + summaries)
    archive = U.sort_units(archive)
    live = set()
    for u in active:
        live |= U.blob_refs(u)
    before = set()
    for u in units_:
        before |= U.blob_refs(u)
    return {"active": active, "archive": archive, "retired": sorted(retired), "summaries": summaries,
            "live_blobs": live, "dropped_blobs": before - live, "fg_ids_sha256": fg_ids_sha256(active + archive)}


def gc_plan(all_blobs, window_gens, durable=True):
    """Blob shas safe to delete: unreferenced by every generation in the rollback window and the dropping
    generation is durable (SPEC_VMEM §7, SW-0180 I3). window_gens: iterable of unit lists (<= VM_GEN_KEEP)."""
    window = list(window_gens)
    if len(window) > VM_GEN_KEEP:
        raise VqaError("E_USAGE", {"reason": "window_too_large", "n": len(window)})
    if not durable:
        return []
    live = set()
    for us in window:
        for u in us:
            live |= U.blob_refs(u)
    return sorted(set(all_blobs) - live)
