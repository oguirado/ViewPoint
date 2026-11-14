# @module {"id":"vm.gates","layer":5,"stage":"S7","owner":"VMEM","deps":["vc.canon","vc.errors","vm.blobs","vm.store"],"book":"HB-VC-s7-vmem"}
"""@module vm.gates - `vmem verify` gates over one generation (SPEC_VMEM §10). Read-only, stdlib, fail-closed.
g_determinism and g_cache_stable need an ingest run; they are ABSENT here and reported under `absent`. g_privacy is a no-op hook."""
import re

from visioncore import canon, ids
from visioncore.errors import VqaError
from . import gen as G
from .blobs import verify_blob, iter_blobs
from .store import Store
from . import compact as C, units as U

UNIT_MAX_B = 1024
DIGEST_MAX_B = 6000
L2_CAP_MIB = 2048
FG_BLOCK_PERMILLE = 900
UNIT_REQUIRED = ("schema", "id", "key", "kind", "layer", "pin", "status", "scope", "tk", "predicate", "anchors")
KINDS = ("baseline", "region_event", "episode", "verdict", "defect")
PINS = ("FAIL", "GOLDEN", "CATALOGUED", None)
GATES = ("g_schema", "g_nofloat", "g_ids", "g_layer_pin", "g_fg_monotone", "g_catalogue", "g_blob_integrity",
         "g_provenance", "g_cite", "g_grid", "g_caps", "g_window", "g_privacy")
ABSENT = ("g_determinism", "g_cache_stable")
_SHA = re.compile(r"^[0-9a-f]{64}$")


def _units(snap, rel="units/active.jsonl"):
    try:
        raw = snap.read(rel).decode("utf-8")
    except VqaError:
        return []
    return [canon.loads(ln) for ln in raw.splitlines() if ln.strip()]


def _g_schema(c):
    for u in c["units"]:
        if any(k not in u for k in UNIT_REQUIRED) or u["schema"] != "vmem.unit/v1" or u["kind"] not in KINDS \
                or u["layer"] not in ("FG", "BG") or u["pin"] not in PINS:
            return False
        U.validate_unit(u)  # full record check (VqaError -> gate fail)
    return True


def _g_nofloat(c):
    for rel in ("units/active.jsonl", "units/archive.jsonl"):
        try:
            _units(c["snap"], rel)  # canon.loads rejects floats
        except VqaError:
            return False
    return True


def _g_ids(c):
    try:
        for u in c["units"]:
            if ids.parse_any(u["id"])[0] != "unit" or ids.parse_any(u["tk"])[0] != "track_key":
                return False
            for a in u.get("anchors", ()):
                if ids.parse_any(a["cite"])[0] != "cross_cite":
                    return False
    except VqaError:
        return False
    return True


def _g_layer_pin(c):
    return all((u["layer"] == "FG") == (u["pin"] is not None) for u in c["units"])


def _g_fg_monotone(c):
    if c["parent"] is None:
        return True
    cur = {u["id"] for u in c["units"] if u["layer"] == "FG"}
    return {u["id"] for u in c["parent_units"] if u["layer"] == "FG"} <= cur


def _g_catalogue(c):
    try:
        rows = c["snap"].read("catalogue/foreground.tsv").decode("utf-8").splitlines()[1:]
    except VqaError:
        return not any(u["layer"] == "FG" for u in c["units"])
    got = sorted(r.split("\t")[0] for r in rows if r)
    return got == sorted(u["id"] for u in c["units"] if u["layer"] == "FG")


def _g_blob_integrity(c):
    items = iter_blobs(c["root"])
    have = {s for s, _ in items}
    if any(not U.blob_refs(u) <= have for u in c["units"]):
        return False  # a referenced blob (FAIL evidence) is gone
    if c["quick"]:
        items = items[:64]
    return all(verify_blob(c["root"], s, e) for s, e in items)


def _g_provenance(c):
    return all(isinstance(u.get("receipt_sha256"), str) and _SHA.match(u["receipt_sha256"]) for u in c["units"])


def _g_cite(c):
    for u in c["units"]:
        if u["kind"] in ("region_event", "defect", "verdict") and u["layer"] == "FG" and not u.get("anchors"):
            return False
        for a in u.get("anchors", ()):
            if a.get("run_id") is None or not str(a.get("cite", "")).startswith(a["run_id"] + "/"):
                return False
    return True


def _g_grid(c):
    for u in c["units"]:
        g = u.get("scope", {}).get("grid")
        lk = u.get("look")
        if lk is not None and lk.get("grid") != g:
            return False
    return True


def _g_caps(c):
    if any(len(canon.dumps(u).encode("utf-8")) > UNIT_MAX_B for u in c["units"]):
        return False
    try:
        if len(c["snap"].read("VISUAL_DIGEST.md")) > DIGEST_MAX_B:
            return False
    except VqaError:
        pass
    fg = c["snap"].manifest.get("fg_blob_bytes")
    if not isinstance(fg, int) or isinstance(fg, bool):
        return False  # commit writes it; a missing counter makes the cap vacuous
    return fg * 1000 <= FG_BLOCK_PERMILLE * L2_CAP_MIB * 1048576


def _g_window(c):
    """Every unit of every generation in [CURRENT-VM_GEN_KEEP+1, CURRENT] has all its blob_refs present."""
    root, cur = c["root"], c["gen"]
    have = {s for s, _ in iter_blobs(root)}
    st = Store(root)
    for n in G.list_gens(root):
        if cur - G.VM_GEN_KEEP + 1 <= n <= cur:  # gens above CURRENT (after a rollback) are ignored
            for u in st.gen_units(n):
                if not U.blob_refs(u) <= have:
                    return False
    return True


def _g_privacy(c):
    return True  # deferred (U-DEC-0005)


def verify(root, quick=False, gen=None):  # @anchor:vm.gates.verify
    """-> {'gates': {name: rc}, 'absent': [...], 'fg_ids_sha256', 'fg_count', 'gen'}; rc 0 or 5 per gate."""
    store = Store(root)
    snap = store.snapshot()
    n = snap.gen if gen is None else gen
    if gen is not None and not G.in_window(root, gen):
        raise VqaError("E_ID", {"reason": "gen_outside_window", "gen": gen})
    if gen is not None and gen != snap.gen:
        from .store import Snapshot
        snap = Snapshot(store, gen)
    try:
        units = _units(snap) + _units(snap, "units/archive.jsonl")
    except VqaError as e:
        raise VqaError("E_GATE", {"reason": "g_nofloat" if e.code == "E_FLOAT" else "g_schema", "cause": e.code})
    parent = snap.manifest.get("parent_gen")
    pu = []
    if parent is not None and parent in G.list_gens(root):
        from .store import Snapshot
        psnap = Snapshot(store, parent)
        pu = _units(psnap) + _units(psnap, "units/archive.jsonl")
    ctx = {"snap": snap, "units": units, "root": str(root), "gen": n, "quick": quick, "parent": parent,
           "parent_units": pu}
    out = {}
    for name in GATES:
        try:
            ok = globals()["_" + name](ctx)
        except (VqaError, KeyError, TypeError, AttributeError):
            ok = False
        out[name] = 0 if ok else 5
    snap.validate()
    fg = sorted(u["id"] for u in units if u["layer"] == "FG")
    return {"gates": out, "absent": list(ABSENT), "gen": n, "fg_count": len(fg),
            "fg_ids_sha256": C.fg_ids_sha256(units)}


def failed(res):
    return sorted(k for k, v in res["gates"].items() if v != 0)
