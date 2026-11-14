# @module {"id":"vm.units","layer":5,"stage":"S7","owner":"VMEM","deps":["vc.canon","vc.errors","vc.ids","vc.layout"],"book":"HB-VC-s7-vmem"}
"""@module vm.units - vmem.unit/v1 records, FG/BG pins, catalogue, look equality (SPEC_VMEM §1, §3, §4). Pure, stdlib only."""
import hashlib
import re
import struct

from visioncore import canon, ids, layout
from visioncore.errors import VqaError

UNIT_MAX_B = 1024
KINDS = ("baseline", "region_event", "episode", "verdict", "defect")
KIND_CODE = {"baseline": "BAS", "region_event": "EVT", "episode": "EPI", "verdict": "VER", "defect": "DEF"}
PINS = ("FAIL", "GOLDEN", "CATALOGUED")
STATUSES = ("active", "superseded", "retired")
PREDICATES = ("baseline", "changed", "moved_from", "frozen", "flicker", "drift", "overlaps", "appeared",
              "vanished", "supersedes", "look_changed")
LINK_RELS = ("moved_from", "supersedes", "same_defect")
ROLES = ("tile", "ui", "actor", "a11y", "object", "gizmo", "island")
SCOPE_RE = {"app": re.compile(r"^[a-z0-9_]{1,32}$"), "scene": re.compile(r"^[a-z0-9_]{1,48}$")}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX16 = re.compile(r"^[0-9a-f]{16}$")
CAT_HEADER = "id\tkey\tpin\ttk\tfirst_run\tlast_run\tsummary\tcrop_sha8"
VM_LOOK_SHARE_PPM = 950000
SIG_HAM_MAX = 3


def _bad(reason, **d):
    d["reason"] = reason
    raise VqaError("E_SCHEMA", d)


def _int(v, lo, hi, name):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        _bad("int_range", field=name)


# --- track keys ---
def track_key(app, scene, W, H, role, m0, m1):
    if role not in ROLES or not (SCOPE_RE["app"].match(app) and SCOPE_RE["scene"].match(scene)):
        _bad("track_key")
    if not (0 <= m0 <= m1 <= 0xFFFF):
        raise VqaError("E_VC_ID_RANGE", {"m0": m0, "m1": m1})
    return ids.track_key(app, scene, W, H, role, m0, m1)


def tk_hash8(tk):
    return hashlib.sha256(tk.encode("utf-8")).hexdigest()[:8]


def unit_key(app, scene, tk, kind):
    return "vision.%s.%s.%s.%s" % (app, scene, tk_hash8(tk), kind)


def scope_key(u):
    s = u["scope"]
    return (s["app"], s["scene"], s["res"], s["grid"])


# --- validation ---
def validate_scope(s):
    if not isinstance(s, dict) or set(s) != {"app", "scene", "res", "grid", "build_seq"}:
        _bad("scope_fields")
    for k in ("app", "scene"):
        if not isinstance(s[k], str) or not SCOPE_RE[k].match(s[k]):
            raise VqaError("E_USAGE", {"scope": k})
    if not isinstance(s["res"], str) or not re.fullmatch(r"[0-9]{1,4}x[0-9]{1,4}", s["res"]):
        _bad("res")
    kind, _ = ids.parse_any(s["grid"])
    if kind != "grid":
        _bad("grid")
    if s["build_seq"] is not None:
        _int(s["build_seq"], 0, 0xFFFFFFFF, "build_seq")


def _validate_anchor(a):
    need = {"run_id", "cite", "frame", "render_index", "bbox_px", "mv_px", "xh64", "sig64", "crop", "desc_blob"}
    if not isinstance(a, dict) or set(a) not in (need, need | {"gop_blob"}):
        _bad("anchor_fields")
    if ids.parse_any(a["run_id"])[0] != "run_id":
        _bad("anchor_run")
    if ids.parse_any(a["cite"])[0] != "cross_cite":
        _bad("anchor_cite")
    _int(a["frame"], 0, 0xFFFFFFFF, "frame")
    if a["render_index"] is not None:
        _int(a["render_index"], 0, 0xFFFFFFFF, "render_index")
    if not (isinstance(a["bbox_px"], list) and len(a["bbox_px"]) == 4) or \
            not (isinstance(a["mv_px"], list) and len(a["mv_px"]) == 2):
        _bad("anchor_geom")
    for v in a["bbox_px"]:
        _int(v, 0, 0xFFFF, "bbox_px")
    for v in a["mv_px"]:
        _int(v, -32768, 32767, "mv_px")
    for k in ("xh64", "sig64"):
        if not isinstance(a[k], str) or not HEX16.match(a[k]):
            _bad("anchor_hash", field=k)
    for k in ("crop", "desc_blob", "gop_blob"):
        if a.get(k) is not None and not (isinstance(a[k], str) and HEX64.match(a[k])):
            _bad("anchor_blob", field=k)


def validate_look(lk, retired=False):
    if not isinstance(lk, dict) or set(lk) != {"grid", "n", "ah64", "desc_blob", "y_q4_mean"}:
        _bad("look_fields")
    if ids.parse_any(lk["grid"])[0] != "grid":
        _bad("look_grid")
    _int(lk["n"], 1, 0xFFFF, "look.n")
    _int(lk["y_q4_mean"], 0, 0xFFFF, "look.y_q4_mean")
    if not (isinstance(lk["ah64"], str) and HEX16.match(lk["ah64"])):
        _bad("look_ah64")
    if lk["desc_blob"] is None:
        if not retired:
            _bad("look_desc_blob")
    elif not (isinstance(lk["desc_blob"], str) and HEX64.match(lk["desc_blob"])):
        _bad("look_desc_blob")


def validate_unit(u):  # @anchor:vm.units.validate_unit
    """Raise VqaError E_SCHEMA (rc 2) unless u is a valid vmem.unit/v1; returns u."""
    if not isinstance(u, dict) or u.get("schema") != "vmem.unit/v1":
        _bad("schema")
    need = {"schema", "id", "key", "kind", "layer", "pin", "status", "born_gen", "last_gen", "scope", "tk",
            "subject", "predicate", "value", "value_num", "value_den", "class", "look", "anchors", "links",
            "verdict", "check_id", "receipt_sha256", "summary"}
    if set(u) != need:
        _bad("fields", missing=sorted(need - set(u)), extra=sorted(set(u) - need))
    kind, f = ids.parse_any(u["id"])
    if kind != "unit" or u["kind"] not in KINDS or f["kind"] != KIND_CODE[u["kind"]]:
        raise VqaError("E_ID", {"id": u["id"], "kind": u["kind"]})
    if not isinstance(u["key"], str) or len(u["key"]) > 96:
        _bad("key")
    if u["layer"] not in layout.UNIT_LAYERS or u["pin"] not in layout.UNIT_PINS:
        _bad("layer_pin")
    if (u["layer"] == "FG") != (u["pin"] is not None):
        _bad("layer_pin_mismatch", id=u["id"])
    if u["status"] not in STATUSES or (u["status"] == "retired" and u["layer"] == "FG"):
        _bad("status", id=u["id"])
    _int(u["born_gen"], 0, 999999, "born_gen")
    _int(u["last_gen"], 0, 999999, "last_gen")
    if u["last_gen"] < u["born_gen"]:
        _bad("gen_order")
    validate_scope(u["scope"])
    if u["tk"] is not None and ids.parse_any(u["tk"])[0] != "track_key":
        _bad("tk")
    if not isinstance(u["subject"], str) or len(u["subject"]) > 48:
        _bad("subject")
    if u["predicate"] not in PREDICATES:
        _bad("predicate")
    if not isinstance(u["value"], str) or len(u["value"]) > 64:
        _bad("value")
    _int(u["value_num"], -(1 << 63), (1 << 63) - 1, "value_num")
    _int(u["value_den"], 1, 0xFFFFFFFF, "value_den")
    if u["class"] not in layout.CLS:
        _bad("class", cls=u["class"])
    retired = u["status"] == "retired"
    if u["look"] is not None:
        validate_look(u["look"], retired)
    if not isinstance(u["anchors"], list) or len(u["anchors"]) > 6:
        _bad("anchors")
    for a in u["anchors"]:
        _validate_anchor(a)
    if not isinstance(u["links"], list) or len(u["links"]) > 4:
        _bad("links")
    for l in u["links"]:
        if not isinstance(l, dict) or set(l) != {"rel", "to"} or l["rel"] not in LINK_RELS:
            _bad("link")
        if ids.parse_any(l["to"])[0] != "unit":
            raise VqaError("E_ID", {"to": l["to"]})
    if u["verdict"] is not None and u["verdict"] not in layout.VERDICTS:
        _bad("verdict")
    if u["check_id"] is not None and (not isinstance(u["check_id"], str) or len(u["check_id"]) > 64):
        _bad("check_id")
    if not isinstance(u["receipt_sha256"], str) or not HEX64.match(u["receipt_sha256"]):
        _bad("receipt")
    if not isinstance(u["summary"], str) or len(u["summary"]) > 160:
        _bad("summary")
    if len(dump_unit(u).encode("utf-8")) > UNIT_MAX_B:
        raise VqaError("E_BUDGET", {"id": u["id"], "limit": UNIT_MAX_B})
    return u


def dump_unit(u):
    return canon.dumps(u)


def load_units(text):
    return [canon.loads(l) for l in text.splitlines() if l.strip()]


def dump_units(units):
    return "".join(dump_unit(u) for u in sort_units(units))


def sort_units(units):
    return sorted(units, key=lambda u: u["id"])


def make_unit(n, kind, scope, tk, predicate="baseline", cls="UNCHANGED", pin=None, gen=1, anchors=None,
              look=None, links=None, verdict=None, check_id=None, receipt_sha256="0" * 64,
              subject=None, value="", value_num=0, value_den=1, summary="", status="active"):
    app, scene = scope["app"], scope["scene"]
    u = {"schema": "vmem.unit/v1", "id": ids.unit_id(KIND_CODE[kind], n),
         "key": unit_key(app, scene, tk, kind), "kind": kind, "layer": "FG" if pin else "BG", "pin": pin,
         "status": status, "born_gen": gen, "last_gen": gen, "scope": scope, "tk": tk,
         "subject": subject if subject is not None else tk_hash8(tk), "predicate": predicate,
         "value": value, "value_num": value_num, "value_den": value_den, "class": cls, "look": look,
         "anchors": anchors or [], "links": links or [], "verdict": verdict, "check_id": check_id,
         "receipt_sha256": receipt_sha256, "summary": summary}
    return validate_unit(u)


# --- FG / BG (SPEC_VMEM §4) ---
def is_fg(u):
    return u["pin"] is not None


def apply_pins(units, fail_cites=()):
    """Pin FAIL (SPEC_VMEM §4): verdict FAIL units, defect units, and every region_event cited by an accepted FAIL
    citation (`fail_cites` plus the anchor cites of the FAIL units). Existing pins are kept. -> new list."""
    cites = set(fail_cites)
    for u in units:
        if u["verdict"] == "FAIL" or u["kind"] == "defect":
            cites |= {a["cite"] for a in u["anchors"]}
    out = []
    for u in units:
        if u["pin"] is None and (u["verdict"] == "FAIL" or u["kind"] == "defect" or (
                u["kind"] == "region_event" and any(a["cite"] in cites for a in u["anchors"]))):
            u = dict(u, pin="FAIL", layer="FG")
        out.append(u)
    return out


def pin_unit(u, pin="CATALOGUED"):
    if pin not in PINS:
        _bad("pin")
    return validate_unit(dict(u, pin=pin, layer="FG"))


def supersede(old, new_id):
    """FG may only go active->superseded (+ last_gen bump); returns the edited copy of old and the link for the successor."""
    if not is_fg(old):
        _bad("not_fg")
    return dict(old, status="superseded"), {"rel": "supersedes", "to": old["id"]}


def unit_run(u):
    return u["anchors"][0]["run_id"] if u["anchors"] else None


def blob_refs(u):
    refs = set()
    for a in u["anchors"]:
        for k in ("crop", "desc_blob", "gop_blob"):
            if a.get(k):
                refs.add(a[k])
    if u["look"] and u["look"]["desc_blob"]:
        refs.add(u["look"]["desc_blob"])
    return refs


# --- catalogue (vmem.catalogue/v1): one row per FG unit ---
def _tsv(s):
    return s.replace("\t", " ").replace("\n", " ")[:120]


def catalogue_rows(units):
    rows = []
    for u in units:
        if not is_fg(u):
            continue
        runs = sorted({a["run_id"] for a in u["anchors"]})
        crop = next((a["crop"] for a in u["anchors"] if a["crop"]), None)
        rows.append("\t".join([u["id"], u["key"], u["pin"], u["tk"] or "", runs[0] if runs else "",
                               runs[-1] if runs else "", _tsv(u["summary"]), crop[:8] if crop else ""]))
    return sorted(rows, key=lambda r: (r.split("\t")[1], r.split("\t")[0]))


def render_catalogue(units):
    return CAT_HEADER + "\n" + "".join(r + "\n" for r in catalogue_rows(units))


def check_catalogue(units, text):
    """g_catalogue: FG units <-> catalogue rows bijection (rc 5 E_GATE)."""
    lines = text.split("\n")
    if not lines or lines[0] != CAT_HEADER:
        raise VqaError("E_GATE", {"reason": "g_catalogue", "why": "header"})
    got = [l.split("\t")[0] for l in lines[1:] if l]
    want = sorted(u["id"] for u in units if is_fg(u))
    if sorted(got) != want or len(set(got)) != len(got):
        raise VqaError("E_GATE", {"reason": "g_catalogue", "missing": sorted(set(want) - set(got)),
                                  "extra": sorted(set(got) - set(want))})
    return True


# --- look equality (SPEC_VMEM §3.2) ---
def _tiles(blob):
    if len(blob) % layout.TILE_SIZE:
        return []
    return [layout.unpack_tile(blob, o) for o in range(0, len(blob), layout.TILE_SIZE)]


def look_eq(a, b, load_blob=None, t0_q4=32, ham_max=SIG_HAM_MAX):
    """-> SAME_EXACT | SAME_NOISY | DIFFERENT. Grid mismatch -> VqaError E_STALE (rc 4)."""
    if a["grid"] != b["grid"]:
        raise VqaError("E_STALE", {"reason": "grid_mismatch", "a": a["grid"], "b": b["grid"]})
    if a["n"] != b["n"]:
        return "DIFFERENT"
    if a["ah64"] == b["ah64"]:
        return "SAME_EXACT"
    if load_blob is None or not a["desc_blob"] or not b["desc_blob"]:
        return "DIFFERENT"
    ta, tb = _tiles(load_blob(a["desc_blob"])), _tiles(load_blob(b["desc_blob"]))
    if len(ta) != a["n"] or len(tb) != b["n"] or not ta:
        return "DIFFERENT"
    match = 0
    for x, y in zip(ta, tb):
        if x["xh64"] == y["xh64"] or (
                (x["sig64"] ^ y["sig64"]).bit_count() <= ham_max and abs(x["y_q4"] - y["y_q4"]) <= t0_q4
                and abs(x["sig_q4"] - y["sig_q4"]) <= t0_q4):
            match += 1
    return "SAME_NOISY" if match * 1000000 >= VM_LOOK_SHARE_PPM * len(ta) else "DIFFERENT"
